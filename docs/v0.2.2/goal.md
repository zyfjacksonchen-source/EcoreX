# EcoreX v0.2.2 Goal

## Objective

v0.2.2 turns the v0.2.1 Web runtime from a working but split-state system into a Codex-like, backend-led runtime:

- Durable backend run events are the canonical source of truth.
- The frontend consumes runtime projections instead of reconstructing agent state with local heuristics.
- Agent work is observable, replayable, auditable, and recoverable after refresh, reconnect, and sidecar restart.
- Stable networks must not show frequent disconnect/reconnect recovery prompts.
- Feishu/Lark and IM channels must report real configured/connected/authenticated/callable state, with real smoke evidence or explicit blockers.

## Development Standard

- Work is split into slices. Each slice must have code, tests, evidence, and review notes.
- The agent or subagent that writes a slice cannot be the reviewer that grants PASS for that slice.
- Each slice requires multi-angle review: backend/runtime, frontend/state, harness/tests, security/audit, and release/regression.
- A slice cannot pass while any P0/P1 finding remains open or while reviewers disagree on a blocking behavior.
- Source/API inspection is not enough for real business closure. Real channel calls, browser smokes, and reconnect scenarios must either pass or be recorded as blockers.
- New runtime logic must not accumulate inside monolithic UI/backend files when a dedicated module is warranted.

## Slices

1. Durable `RunEventLedger` with idempotent append, ordered replay, and test reset helpers.
2. Runtime projection service for session messages, active requests, Run Center, audit timeline, tool/artifact/permission state.
3. WebChannel event emission wired through the durable ledger while preserving legacy SSE compatibility.
4. Frontend runtime consumer that renders backend projection and demotes legacy history merge to fallback.
5. Reconnect governance based on durable cursors and measurable health, not cosmetic recovery text.
6. Agent observation context, capability epoch, tool/subagent heartbeat, and external-capability convergence rules.
7. Backend/frontend harnesses for replay, refresh, disconnect, permissions, artifacts, channels, and Feishu.
8. Feishu/Lark and IM channel real closure: Bot channel state separated from `feishu_cli` connector state.
9. Memory/Knowledge/Channels UI status surface with honest configured/connected/callable/auth states.
10. Artifact, copy, Markdown, compact thinking, and menu interaction stabilization.
11. Admin logs/sync and diagnostic bundle sourced from runtime events.
12. Deployment, flags, migration, rollback, evidence ledger, and release review.
13. Image generation optimization from source thread `019ef9a8-7344-7712-beb9-f9008dd90622`: provider routing, async image jobs, incremental artifacts, OCR reuse, bounded concurrency, retry/fallback telemetry, and projection-based recovery. This slice is subordinate to the v0.2.2 backend-led runtime direction.
14. Scheduler visibility and management: backend-owned scheduler runtime/task projection, lazy runtime initialization when the scheduler ability is enabled, and a frontend management surface that only consumes projection and sends bounded backend commands.
15. Project-session parity and ownership isolation: new project chats must behave like general chats for composer autosize, send lifecycle, active-session isolation, and project binding; project messages must not leak into the general-session stream/list; general sessions cannot be dragged into project sessions and project sessions cannot be dragged back into general sessions.
16. Codex-like textual status motion: replace broad block shimmer/sweeping light with a restrained text-level sweep on status copy, with no large background wash.
17. Independent Web Markdown-it full migration: Web streaming, final answers, history replay, projection refresh, long-answer preview/full view, tool/content-step Markdown, memory/knowledge Markdown viewers, and media preview rewrites must use the CowAgent Web console `markdown-it`/`highlight.js` renderer contract end to end. Streaming must not expose transient raw `#` headings or malformed list/table/code-fence syntax. Final and streaming output must restore the rendering/output/layout style of `zhayujie/CowAgent.git`, including symbols, emoji, spacing, headings, lists, tables, links, media previews, and code blocks. Desktop is out of scope for this slice.
18. Run Center user hiding: ordinary frontend surfaces must not expose Run Center navigation, modals, settings panels, toast copy, or user-facing labels; runtime diagnostics remain backend/audit-visible only.

## Hard Gates

- No duplicate user/assistant turns after refresh, reconnect, retry, or history reload.
- New running turns can be reconstructed from backend projection without frontend content-key guessing.
- `assistant.delta` means append; `assistant.snapshot` and `message.assistant.finalized` mean replace/finalize.
- Stable service and network must not produce frequent "connection interrupted/recovering" UI.
- Feishu/IM channels must never report active or authenticated only because a catalog entry exists.
- Image generation must not create a parallel frontend-only task state; image job progress and artifacts must be recoverable from backend runtime events and projections.
- Scheduler state must not be inferred from frontend toggles or tool availability alone; visible status, tasks, next runs, failures, and permission blockers must come from the backend scheduler projection.
- Project and general sessions must be isolated by backend session/project identity; frontend convenience state and drag/drop interactions must not cause cross-session message bleed or ownership migration.
- Web streaming Markdown must prefer stable rendered blocks and hidden/inert incomplete markers over showing raw syntax fragments such as a lone `#`, dangling list marker, partial table delimiter, or raw code fence marker.
- Web Markdown must not fork into multiple parsers or hand-rolled final renderers; every ordinary answer surface must be traceable to the CowAgent-compatible `renderMarkdown(...)`/`renderStreamingMarkdown(...)` contract, with bounded exceptions documented in [web-markdown-it-migration-plan.md](web-markdown-it-migration-plan.md).
- Ordinary users must not see Run Center entry points or copy by default.
- No P0/P1 blockers remain before release; any missing real credential must be recorded as `BLOCKER-PENDING-CREDENTIALS`.

## Image Generation Slice Acceptance

- Provider config: custom image provider key/base routes through explicit image config and never leaks secrets; OpenAI/LinkAI retain `gpt-image-2-pro` default and visible `gpt-image-2` fallback.
- Async jobs: Web/Desktop uses `ImageJobService` for start/status/collect/cancel; non-SSE/IM paths retain final synchronous compatibility.
- Incremental artifacts: each completed image emits `artifact.created` and `image_job.artifact` without waiting for all images.
- Observability: image jobs emit started/progress/completed/failed/cancelled runtime events with sanitized provider/model/retry/fallback/timing telemetry.
- Scenarios: single generation, multi-generation, single edit, fused-reference multi-image edit, per-image edit, multi-intent DAG, and OCR-brief reuse are covered by harness tests.
- Recovery: RuntimeProjection can rebuild image job progress and artifacts after refresh/reconnect without duplicate artifacts.
- Conflict rule: if speed optimizations conflict with canonical runtime events/projection, the canonical runtime event path wins.

## Scheduler Slice Acceptance

- Projection: `/api/scheduler` returns scheduler enablement, initialization, running/thread state, task-store path, task counts, safe task projections, next/last run timestamps, last errors, and permission blockers.
- Lifecycle: enabling the scheduler through optional abilities or the scheduler UI can lazily initialize the task store/service before a scheduler tool call reports "not initialized".
- Management: frontend exposes current scheduled tasks and bounded actions for start/stop, enable/disable, rename, cron update, content update, delete, and refresh.
- Source of truth: frontend never owns task truth; every mutation returns a fresh backend projection and failed mutations still leave the latest projection observable.
- Security/audit: task projections hash receiver identity, mask tool/skill secrets, and POST mutations recheck the permission broker server-side.
- Harness: backend projection/API tests, scheduler-tool lazy-init test, read-only regression, Web source contracts, JavaScript/Python syntax checks, and browser smoke must pass or be recorded as pending before reviewer handoff.

## Frontend UX And Rendering Slice Acceptance

- Project sessions: creating a project chat immediately binds the session to the intended project, composer height autosizes identically to general chats, pending sends stay in the project session, reload/history never moves those messages into the general list, and drag/drop cannot move sessions across project/general ownership boundaries.
- Status motion: "connecting/processing" states use a text-mask sweep over the glyphs only, respect reduced-motion, avoid wide gradient bands, and do not shift layout.
- Web-only scope: desktop renderer/component changes are explicitly out of scope for this slice unless a later user request reopens desktop work.
- Markdown baseline: implementation must follow [web-markdown-it-migration-plan.md](web-markdown-it-migration-plan.md), based on `cowagent-origin/master` Web console `markdown-it` plus `highlight.js` behavior.
- Streaming Markdown: headings/lists/tables/code fences render only when syntactically stable; incomplete markers are buffered, hidden, or rendered as plain inert pending text without exposing raw `#`, dangling list syntax, partial table delimiters, or raw triple backticks.
- Symbols/emoji: the renderer must preserve original symbols, emoji, punctuation, and line breaks; sanitization must not strip visible user/assistant content.
- Full migration: every Web answer surface, including streaming, final message, history replay, long-answer preview/full view, content steps, memory/knowledge Markdown viewers, and media preview rewrites, must route through the same CowAgent-compatible renderer contract or document a bounded exception.
- Run Center: source/static tests and browser smoke must prove no ordinary UI element contains Run Center labels or opens Run Center unless an explicit internal developer flag is enabled outside normal user paths.
- Review: Markdown rendering requires parallel multi-agent discussion before implementation review; implementer does not self-review.

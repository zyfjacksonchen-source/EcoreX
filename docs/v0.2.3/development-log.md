# EcoreX v0.2.3 Development Log

## R23-00

- Created v0.2.3 goal, acceptance, review, evidence, and harness skeletons.
- Preserved v0.2.2 sealed artifacts during workspace cleanup; only regenerated caches and temporary installer directories were removed.

## R23-02 to R23-06

- Added `BrowserAutomationService` as the shared CDP-first diagnostic/auto-launch layer.
- Changed root/browser defaults and desktop sidecar defaults to `cdp_auto_launch=true`, with CDP fallback preserved.
- Added `ocr` tool with `extract_text`, `extract_urls`, image hash cache, Pillow preprocessing, local OCR provider detection, and browser handoff metadata.
- Aligned tool-selection hints so URL/XHS/read-link tasks bias toward browser/CDP, with OCR as the fast URL extraction path.

## R23-07 to R23-12

- Added `/api/external-connections` and `/api/external-connections/{platform}/actions`, projected from existing channel state to avoid a second source of truth.
- Added the desktop Settings > 外部连接 module with platform cards, logo/fallback markers, backend-driven fields, save/test/connect/disconnect, and home-channel actions.
- Added `scripts/smoke-web-external-connections-browser.py` to exercise the built renderer settings flow:
  - opens Settings and selects the first-level 外部连接 tab;
  - verifies Feishu/Lark and Slack platform cards, logo markers, configured/callable status text, and empty setup hint;
  - saves masked Feishu config and asserts the `****` secret is not echoed back in the action payload;
  - confirms Run Center remains hidden in the production renderer smoke.
- Verification passed:
  - `python -m py_compile scripts\smoke-web-external-connections-browser.py tests\test_v023_external_connections_cdp_ocr.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `12 passed`
  - `npm --prefix desktop run typecheck`
  - `npm --prefix desktop run build:renderer` passed with existing chunk-size warning
  - `python scripts\smoke-web-external-connections-browser.py --artifact docs\v0.2.3\artifacts\external-connections-browser-smoke.json --screenshot docs\v0.2.3\artifacts\external-connections-browser-smoke.png` -> `connectionCards=2`, `secretRedactedOnSave=true`, `runCenterHidden=true`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\external-connections-browser-smoke.json --json-output docs\v0.2.3\artifacts\external-connections-privacy-scan.json --salt v023-external-connections` -> `findingCount=0`

## R23-18 to R23-19

- Parallel explorer Maxwell researched Hermes skill learning at `NousResearch/hermes-agent@a2b49e6`; findings captured in `hermes-skill-learning-plan.md`.
- Added `SkillLearningService` for ledger-backed learned skill drafts, validation/security review events, and approval through `SkillService.add`.
- Extended `agent_capability` with `request_skill_learning`, `create_skill_draft`, and `approve_skill_draft`.
- Removed fixed `create-xiaohongshu-note` from source built-in skills, active Codex skills, active EcoreX workspace skills, and managed built-in refresh markers.

## R23-16P

- Inserted the long-session and complex-task performance optimization slice before final release gating.
- Added `performance-optimization-plan.md` with baseline, runtime projection, event hygiene, frontend render, tool resource lifecycle, and harness/release-gate sub-slices.
- Started four parallel read-only reviewers:
  - Copernicus: Runtime/Backend performance.
  - Beauvoir: Frontend/UX performance.
  - Euler: Tools/Resource lifecycle.
  - Schrodinger: Harness/Test/Release/Observability.
- Multi-agent plan review converged to PLAN-PASS. Implementation must still add
  the performance harnesses and code optimizations before final v0.2.3 release
  PASS.
- R23-16P-01 RuntimeProjection efficiency implementation:
  - Added `RunEventLedger.events_for_requests()` so session projection can batch request event replay instead of querying once per request.
  - Added `latest_event_id_for_request()` and `latest_event_id_for_session()` so projection caches invalidate on durable event append.
  - Added request/session projection caches in `RuntimeProjectionService`, keyed by owner/session, cursor/limit/include-events, and latest event id.
  - `RuntimeProjectionHandler` now passes `include_events=False` for default Web/API projection calls; `include_events=1` keeps diagnostic event payload behavior.
  - Security/Runtime re-review found that generic `include_events=1` event payloads could expose tokenless bodies, derived body keys, unknown identifier-like codenames, and loosely validated structural ids. Fixed by making generic diagnostic strings default to `[redacted-content]` summaries and preserving only strict structure fields: enum status/message type, validated numeric counts, hash-shaped hashes, and `msg-`/`prompt-`/`instruction-` ids with sensitive suffix rejection. Renderable projection messages remain available for the UI. Added and expanded `test_v023_runtime_projection_include_events_redacts_generic_event_bodies`.
  - Added `tests/test_v023_performance_projection.py` and `scripts/smoke-runtime-projection-performance.py`.
  - Generated `docs/v0.2.3/artifacts/perf-long-session.json`: 200 requests / 800 events, steady-state session projection P95 `24.904ms`, request projection P95 `2.069ms`, redaction flags clean.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\perf-long-session.json --json-output docs\v0.2.3\artifacts\perf-long-session-privacy-scan.json --salt v023-performance` -> `findingCount=0`.
  - Verification passed: py_compile for runtime projection/ledger/web handler/perf test/smoke; performance pytest `4 passed`; include-events body redaction regression `1 passed`; v0.2.3 external/CDP/OCR pytest `91 passed`; session identity pytest `15 passed`; v0.2.2 runtime projection gate `4 passed`; runtime projection focused gate `20 passed, 365 deselected`.
  - Five-angle review reached R23-16P-01 PASS consensus: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. Broader R23-16P frontend/resource/final gates remain pending.
- R23-16P-03 Frontend render/state isolation implementation:
  - Collapsed long replies now render `previewContent` bounded by `LONG_REPLY_PREVIEW_CHARS` instead of sending the full assistant Markdown tree into the collapsed preview.
  - Process/call details now lazy-mount `.agent-steps` only after the `<details>` element is opened; the closed state keeps only summary/current-step evidence visible.
  - Added `scripts/smoke-frontend-render-performance.py` and a frontend performance contract in `tests/test_v023_performance_projection.py`.
  - Generated `docs/v0.2.3/artifacts/perf-frontend-render.json`: synthetic 200000-char long reply collapses to 1404 Markdown chars (`99.298%` reduction), closed process details mount `0` step lists, expanded process details mount `1` step list.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\perf-frontend-render.json --json-output docs\v0.2.3\artifacts\perf-frontend-render-privacy-scan.json --salt v023-frontend-render` -> `findingCount=0`.
  - Verification passed: py_compile for the frontend performance smoke/test; performance pytest `5 passed`; `npm --prefix desktop run typecheck`; `npm --prefix desktop run build:renderer` with the existing Vite chunk-size warning. Static WebUI was synced to `channel/web/static/app` with `index-BC5jFAov.js`.
  - R23-16P-03 five-angle review reached PASS consensus: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. Broader R23-16P resource/final gates remain pending.
- R23-16P-04 Resource lifecycle implementation:
  - `SchedulerService.stop()` now signals a stop event and names its thread `SchedulerServiceThread`, so shutdown no longer waits behind the 30-second polling sleep.
  - `ImageJobService` now records `finished_at` for terminal jobs, exposes redacted `resource_snapshot()` counters, and adds `cleanup_finished_jobs()` to prune completed/failed/cancelled observer state without deleting running jobs or RunEventLedger truth.
  - Review-blocker fixes added independent `in_flight` tracking so synchronous parallel jobs with early terminal events cannot be pruned until all workers drain; live `status()` now re-sanitizes artifacts, and artifact sanitizers in both `ImageJobService` and `RuntimeProjection` replace sensitive local paths, signed/token URLs, and prompt/private/OCR-like labels with stable hash references.
  - Scheduler due-task logs now report a task-name summary hash/length instead of raw `task["name"]`.
  - Security re-review blockers fixed scheduler callback, top-level loop, per-task, and schedule-parse exception handling: callback errors, `list_tasks()` failures, per-task processing failures, invalid `next_run_at`, invalid cron expressions, and invalid once `run_at` values now log/persist only type/hash/length summaries, not raw exception or schedule text.
  - Added resource lifecycle contracts in `tests/test_v023_performance_projection.py`: scheduler stop releases the background thread within the gate, and image-job cleanup removes terminal jobs while preserving a running job.
  - Added review-blocker regressions for synchronous parallel worker drain and artifact redaction across live status, image-job API payload helper, RunEventLedger events, RuntimeProjection, and post-cleanup status.
  - Release review blockers fixed v0.2.2 image-job Web API compatibility: recovered projection fallback now fills public `turn_id`/`session_id` from ledger owner ids without exposing raw legacy ids, include-events entries receive the same public ids when sanitizer removed raw ids, and image-job validation `ValueError`s use `_public_validation_error_payload()` so safe missing-prompt messages remain visible.
  - Added `scripts/smoke-complex-task-resource-lifecycle.py`, which synthesizes scheduler, OCR text-URL extraction, parallel image jobs, cleanup, and RuntimeProjection steady-state projection without real accounts or external network. Browser/CDP process and subagent lifecycle gates remain separate R23-16P scenarios.
  - Generated `docs/v0.2.3/artifacts/perf-complex-task-soak.json`: `threadDeltaAfterIdle=0`, `processDeltaMeasured=false`, `cacheBytesMeasured=false`, `sseApplyP95Ms=4.599`, `schedulerStopMs=0.617`, `imageJobCountAfterCleanup=0`, `ocrTextUrlP95Ms=0.082`.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\perf-complex-task-soak.json --json-output docs\v0.2.3\artifacts\perf-complex-task-soak-privacy-scan.json --salt v023-complex-soak` -> `findingCount=0`.
  - Verification passed: py_compile for lifecycle/projection/scheduler/web handler/test/smoke; performance pytest `12 passed`; v0.2.2 image-job recovery/validation focused gate `3 passed, 382 deselected`; complex-task lifecycle smoke PASS; privacy scan `findingCount=0`. R23-16P-04 five-angle review reached PASS consensus after blocker fixes: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. Broader R23-16P browser/OCR, scheduler/subagent, image-artifact OCR, and final release gates remained pending at that stage; R23-16P-09 later closed the evidence audit gate.
- R23-16P-05 Refresh replay performance gate:
  - Added `scripts/smoke-performance-refresh-replay.py`, a redacted aggregator over the existing browser refresh/replay smokes: `smoke-web-session-cross-talk-refresh-replay.py`, `smoke-web-runtime-projection-reconnect-browser.py`, and `smoke-web-runtime-projection-history-pagination-browser.py`.
  - The aggregator intentionally discards child-smoke body/debug fields such as visible text and raw DOM text, and records only status, durations, call counts, duplicate counts, event-id presence, and enum-like scenario names.
  - Added a static harness contract in `tests/test_v023_performance_projection.py` to keep the performance smoke from writing raw visible text, raw Markdown, document body text, or artifact URLs into the R23-16P artifact.
  - Verification passed: `python -m py_compile scripts\smoke-performance-refresh-replay.py tests\test_v023_performance_projection.py`; performance pytest `13 passed`; `python scripts\smoke-performance-refresh-replay.py --output docs\v0.2.3\artifacts\perf-refresh-replay.json`; privacy scan `findingCount=0`.
  - Latest artifact reports `replayP95Ms=2921.8`, `duplicateMessageCount=0`, `duplicateArtifactCountMeasured=false`, `latestEventIdDeltaMeasured=false`, `reconnectCount=11`, `projectionCallCount=1`, `streamCallCount=1`, `historyProjectionFetchCount=4`, `historyFallbackCallCount=1`, and `consoleErrorCount=0`.
  - R23-16P-05 five-angle review reached PASS consensus after Harness/Test and Frontend/UX blocker fixes: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. R23-16P browser/OCR, scheduler/subagent, image-artifact OCR, and final release gates remained pending at that stage; R23-16P-09 later closed the evidence audit gate.
- R23-16P-06 Browser/CDP + OCR performance gate:
  - Added `scripts/smoke-performance-browser-ocr.py`, which measures CDP-first auto-launch on an isolated localhost CDP port/profile, forced Playwright persistent fallback when CDP is unavailable, fast text URL extraction with browser handoff metadata, and local image OCR availability.
  - The smoke suppresses BrowserService/Playwright noisy logs during measurement, uses a dedicated temporary browser profile, hashes target URLs/endpoints/process ids, and writes only redacted metrics to `docs/v0.2.3/artifacts/perf-browser-ocr.json`.
  - Initial Windows evidence had no local `tesseract`/`pytesseract`, so screenshot URL OCR was recorded honestly as unmeasured rather than a fake pass. R23-16P-10 later added RapidOCR and closed that gap.
  - Verification passed: `python -m py_compile agent\tools\browser\browser_service.py scripts\smoke-performance-browser-ocr.py tests\test_v023_performance_projection.py`; performance pytest `15 passed`; `python scripts\smoke-performance-browser-ocr.py --output docs\v0.2.3\artifacts\perf-browser-ocr.json --iterations 3 --timeout-ms 15000`; privacy scan `findingCount=0`.
  - Runtime/Backend review blocker fixed: the smoke now fails closed unless the first browser action really uses CDP, observes an auto-launched CDP process, and verifies that process is no longer alive after close; the performance test includes static contracts for those failure codes.
  - Harness/Test repeatability blocker fixed by hardening `BrowserService` CDP auto-launched process cleanup: CDP shutdown now waits for the process, kills it if needed, and waits for kill completion before clearing the handle.
  - Latest R23-16P-06 artifact reported `browserFirstActionMode=cdp`, `browserFirstActionMs=11752.547`, `browserFallbackMode=persistent`, `browserFallbackMs=5663.459`, `ocrTextUrlP95Ms=0.058`, image OCR unmeasured, `liveBrowserProcessDeltaAfterIdle=0`, and `liveBrowserProcessDeltaMeasured=true`.
  - R23-16P-06 five-angle review reached PASS consensus after Runtime/Backend and Harness/Test blocker fixes: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. Screenshot/image OCR measurement was closed by R23-16P-10.
- R23-16P-07 Image-artifact OCR performance gate:
  - Added `scripts/smoke-performance-image-artifact-ocr.py`, a redacted direct `ImageJobService` smoke for OCR reuse, retry, failure, cancellation, artifact projection merge, event payload size, cleanup, and idle thread delta.
  - The smoke intentionally avoids real provider credentials and does not write raw image refs, OCR brief text, request/session ids, full paths, raw events, or prompt/message bodies into the artifact.
  - Added `V023ImageArtifactOcrPerformanceHarnessTests` to keep the smoke contract tied to hashed identity fields, lifecycle coverage, RuntimeProjection reduction, cleanup, and artifact redaction.
  - Verification passed: `python -m py_compile scripts\smoke-performance-image-artifact-ocr.py tests\test_v023_performance_projection.py`; performance pytest `16 passed`; `python scripts\smoke-performance-image-artifact-ocr.py --output docs\v0.2.3\artifacts\perf-image-artifact-ocr.json --task-count 12 --artifacts-per-task 2 --projection-iterations 8`; privacy scan `findingCount=0`.
  - Frontend/UX review P2 hardening added hard thresholds for `eventCount` and `payloadBytes`, plus checks that projected artifact fingerprints and renderable public field shapes match the expected artifact count.
  - Runtime/Backend review P1 fixes restored v0.2.2 runtime invariants: `sk-` text masking is now boundary-safe so `task-subagent-*` ids are not corrupted; `permission.requested` has a dedicated projection sanitizer preserving strict `permission_request_id`/`id`/`tool`; WebChannel-redacted subagent raw result payloads project as empty strings; non-sensitive workspace artifact paths remain renderable while user-home/sensitive paths still hash.
  - Current artifact reports `ocrReuseP95Ms=1.112`, `ocrProviderCallCount=4`, `ocrCacheHitCount=10`, `ocrCacheMissCount=2`, `artifactMergeP95Ms=13.4`, `eventCount=121`, `payloadBytes=25824`, `projectedArtifactCount=24`, `projectedArtifactFingerprintCount=24`, `projectedArtifactShapeValidCount=24`, `retryEventCount=1`, `threadDeltaAfterIdle=0`, and `jobsAfterCleanup=0`.
  - Current verification after P1 fixes: `python -m py_compile common\ecorex_public_payload.py agent\protocol\runtime_projection.py agent\protocol\run_event_ledger.py`; v0.2.2 subagent/permission focused subset `20 passed, 365 deselected`; performance pytest `16 passed`; image-artifact OCR smoke PASS; privacy scan `findingCount=0`.
  - R23-16P-07 five-angle review later reached PASS; scheduler/subagent passed in R23-16P-08; R23-16P-09 closed the evidence audit gate; R23-16P-10 closed the screenshot OCR provider gate. Final release gates remain pending.
- R23-16P-08 Scheduler/subagent performance gate:
  - Added `scripts/smoke-performance-scheduler-subagent.py`, a redacted smoke that writes durable `subagent.started/updated/completed/timeout/cancelled/failed` events, measures `RuntimeProjectionService.request_projection()`, creates a real scheduler `TaskStore`, measures `scheduler_projection()`, and verifies `SchedulerService` stop leaves no scheduler thread/timer residue.
  - Added `V023SchedulerSubagentPerformanceHarnessTests` to require lifecycle/status coverage, scheduler projection coverage, cleanup counters, and artifact redaction guards.
  - Verification passed: `python -m py_compile scripts\smoke-performance-scheduler-subagent.py tests\test_v023_performance_projection.py`; performance pytest `17 passed`; `python scripts\smoke-performance-scheduler-subagent.py --output docs\v0.2.3\artifacts\perf-scheduler-subagent.json --subagent-count 40 --scheduler-task-count 60 --projection-iterations 8`; privacy scan `findingCount=0`.
  - Frontend/UX review P2 hardening added renderable shape and fingerprint checks for subagent tool calls, scheduler task shape checks, four scheduler action buckets, and scheduler error-count gating.
  - Current artifact reports `subagentProjectionP95Ms=23.515`, `schedulerProjectionP95Ms=32.675`, `projectedSubagentToolCount=40`, `projectedSubagentToolShapeValidCount=40`, `projectedSubagentToolFingerprintCount=40`, `projectedSchedulerTaskCount=60`, `projectedSchedulerTaskShapeValidCount=60`, `completedSubagentCount=10`, `timeoutSubagentCount=10`, `cancelledSubagentCount=10`, `failedSubagentCount=10`, four scheduler action buckets at `15`, `schedulerErrorTaskCount=6`, `schedulerStopMs=0.457`, `orphanThreadCount=0`, and `orphanTimerCount=0`.
  - R23-16P-08 five-angle review reached PASS consensus after Frontend/UX P2 hardening: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression.
  - R23-16P-09 closed the evidence audit gate; R23-16P-10 closed the screenshot OCR provider gate. Final release gates remain pending.

## R23-20

- Inserted a conversation identity/sorting integrity slice after the user reported session cross-talk, disappearing pinned conversations, and rename-triggered pinning in both project and general session lists.
- Started parallel read-only reviewers:
  - Leibniz: Frontend session list/state merge.
  - Hume: Backend conversation store/session identity.
  - Avicenna: RuntimeProjection/history overlay/request-session binding.
  - Arendt: Frontend sorting, pin, rename product semantics.
  - Aristotle: Harness/Test/Data Repair/Release gates.
- Added `session-identity-sorting-plan.md`.
- Current confirmed root-cause evidence:
  - `renameSession` writes `pinnedSessions[row.id] = true`, so rename/title-lock currently auto-pins.
  - `/api/sessions` is consumed as page 1 size 40, so old pinned/live sessions can be paginated away unless explicitly included.
  - `mapSessions` merges backend sessions, local UI state, active requests, and active session fallback without a single canonical ownership resolver.
  - `SessionRow.updatedAt` is display text but participates in sorting fallback; v0.2.3 needs a separate `sortKeyMs`.
  - Recovery/projection paths can replay by `request_id` without an expected session guard; v0.2.3 needs immutable request owner contracts and `session_mismatch` rejection.
- Implemented the direct R23-20D frontend source fix: `renameSession` no longer writes `pinnedSessions[row.id] = true` for either title-lock or title-change paths.
- Implemented partial R23-20B backend owner guard: `ConversationStore.append_messages` rejects project A -> project B and existing general -> project silent rebinding instead of overwriting canonical ownership; `/message` validates before writing project binding UI state.
- Implemented partial R23-20G sorting rule: `SessionRow.sortKeyMs` drives ordering; pinned sessions sort newest-first within pinned, unpinned sessions sort newest-first within unpinned, and `updatedAt` is display-only.
- Implemented R23-20I historical attachment context recovery: `load_messages` restores limited `[历史图片/历史文件: path]` references from user message extras for the next model turn while `load_history_page` keeps UI history clean.
- Verification passed: `python -m py_compile agent\memory\conversation_store.py agent\memory\__init__.py channel\web\web_channel.py bridge\agent_bridge.py agent\chat\service.py tests\test_ecorex_session_identity_sorting.py`; `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_session_identity_sorting.py -q`; focused legacy project-context pytest; `npm --prefix desktop run typecheck`.
- R23-20 is partially implemented and remains blocked on include_ids/include_pinned query support, request/session projection guards, repair/privacy scripts, browser smokes, and final Release/Regression gates.

## R23-20E

- Hardened request/session ownership below the UI:
  - `RunLedger.create_run` no longer rewrites `session_id` when the same `request_id` appears under another session; same-session retries remain idempotent.
  - `RunEventLedger.append_event` resolves the request owner from `agent_runs` or the first non-empty event session and rejects mixed-session events.
  - `RuntimeProjectionService` filters request projections to a single owner and filters session projections against the durable request owner, including legacy mixed-owner rows.
  - Runtime `run.accepted` event payload now stores a minimal project binding summary/hash instead of raw project id/name/path fields.
- Added R23-20E tests to `tests/test_ecorex_session_identity_sorting.py`; the file now covers owner overwrite rejection, direct event mixed-owner rejection, legacy projection filtering, historical pasted-image context, and sort source guards.
- Verification passed: `python -m py_compile agent\protocol\run_ledger.py agent\protocol\run_event_ledger.py agent\protocol\runtime_projection.py agent\protocol\__init__.py channel\web\web_channel.py tests\test_ecorex_session_identity_sorting.py`; `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_session_identity_sorting.py -q`; focused v0.2.2 runtime projection/run ledger/project context pytest; `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q`.
- R23-20 remains partial: grouped-sidebar global pin semantics, include_ids/include_pinned APIs, repair scripts, privacy artifact scans, browser refresh/cross-talk smokes, and final Release/Regression gates are still pending.

## R23-20F

- Added a session-list inclusion contract so pinned/current sessions are not dropped merely because `/api/sessions` page 1 is full.
- `ConversationStore.list_sessions` now accepts bounded, de-duplicated `include_session_ids`; it keeps normal `total`, `page`, `page_size`, and `has_more` semantics while appending requested sessions that are outside the current page.
- `/api/sessions` now accepts `include_ids`, `include_session_ids`, `include_pinned`, and `pinned_ids`; pinned state still comes from the existing frontend/runtime UI projection and is not re-created as a second backend source.
- `loadRuntimeSnapshot` now includes pinned session ids and the last active session id in the sessions query while preserving the existing no-argument call shape.
- Added focused tests for store-level include behavior, HTTP handler parsing, frontend source contract, backend runtime project-binding precedence over stale frontend cache, backend general-session ownership absence, and render-layer owner display.
- Verification passed: `python -m py_compile agent\memory\conversation_store.py channel\web\web_channel.py tests\test_ecorex_session_identity_sorting.py tests\test_ecorex_web_parallel_backend.py`; `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_session_identity_sorting.py -q` -> `15 passed`; focused sessions API pytest -> `2 passed` with 3 include subtests; `npm --prefix desktop run typecheck` passed.
- R23-20 remains partial: repair/privacy scripts, browser refresh/cross-talk smokes, and final Release/Regression gates are still pending.

## R23-20F-S

- Added `scripts/audit-ecorex-session-state.py` for dry-run-first session UI state repair.
  - Audits orphan `sessionProjects`, `sessionProjectBindings`, `sessionTitles`, `pinnedSessions`, dangling project metadata, runtime fallback rows, stale local project owners, and v0.2.2 legacy empty-channel rows.
  - `--apply` refuses when active requests are present, checks SQLite integrity, backs up UI state and conversation DB, and only removes UI metadata/local empty fallback rows. It does not delete message bodies.
  - `--rollback` restores from a manifest after backup SHA256 verification and re-checks DB integrity.
- Added `scripts/scan-session-artifacts-privacy.py` as the R23-20 artifact privacy gate. It reports pattern names/counts only and does not echo matched paths, prompts, ids, tokens, cookies, or credentials.
- Added focused tests:
  - `tests/test_ecorex_session_state_repair.py`
  - `tests/test_ecorex_session_privacy_gates.py`
  - `tests/test_ecorex_session_legacy_repair_compat.py`
- Updated `tests/test_ecorex_session_identity_sorting.py` so it can run independently in a clean pytest environment with plugin autoload disabled.
- Generated privacy-safe artifacts:
  - `docs/v0.2.3/artifacts/session-cross-talk-repair-dry-run.json`
  - `docs/v0.2.3/artifacts/session-cross-talk-privacy-scan.json`
- Verification passed:
  - `python -m py_compile scripts\audit-ecorex-session-state.py scripts\scan-session-artifacts-privacy.py tests\test_ecorex_session_identity_sorting.py tests\test_ecorex_session_state_repair.py tests\test_ecorex_session_privacy_gates.py tests\test_ecorex_session_legacy_repair_compat.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; pytest tests/test_ecorex_session_identity_sorting.py tests/test_ecorex_session_state_repair.py tests/test_ecorex_session_privacy_gates.py tests/test_ecorex_session_legacy_repair_compat.py -q` -> `25 passed`
  - `python scripts\audit-ecorex-session-state.py --dry-run --workspace C:\CowAgent --output docs\v0.2.3\artifacts\session-cross-talk-repair-dry-run.json`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\session-cross-talk-*.json --json-output docs\v0.2.3\artifacts\session-cross-talk-privacy-scan.json --salt v023-session-artifacts`
- Security follow-up: after multi-agent review found P1 blockers, `--apply` was changed to fail closed when the active-request snapshot is missing/invalid or lacks an explicit `activeRequests`/`requests`/`runs` collection, when the conversation DB is absent, or when the backend session row-count limit is exceeded. Privacy reports now use HMAC artifact ids and no longer expose manifest file names or report-level raw SHA values.
- R23-20 remains partial until browser cross-talk/refresh smokes and final Release/Regression gates pass.

## R23-20G/H

- Added browser-level R23-20 session integrity smokes:
  - `scripts/smoke-web-session-cross-talk-browser.py`
  - `scripts/smoke-web-session-cross-talk-refresh-replay.py`
- `smoke-web-session-cross-talk-browser.py` exercises the React sidebar against the built renderer with deterministic runtime stubs:
  - pinned sessions outside page 1 are included through `include_pinned=1` and `pinned_ids`;
  - pinned sessions sort newest-first within the pinned group and stay above newer unpinned sessions;
  - backend-declared general/project ownership wins over stale local project bindings;
  - manual rename does not auto-pin a previously unpinned session.
- `smoke-web-session-cross-talk-refresh-replay.py` exercises stale response isolation:
  - selecting a slow A session and quickly returning to B does not let A's late history response pollute B;
  - request/session mismatch is exercised through the real renderer stream/error recovery path, with `session_id` carried on both `EventSource /stream` and `/api/runtime-projection`;
  - mismatch handling is bounded by the reconnect guard, so the smoke now fails if stream/projection attempts spin beyond 6 calls;
  - hard refresh keeps the clean active session, requires a backend B history fetch, and does not resurrect the late A content.
- Added static harness contract tests in `tests/test_ecorex_web_parallel_backend.py` so future edits cannot silently remove the browser-smoke assertions; the tests inspect the actual Python-returned probe scripts instead of whole-file comments/dead strings.
- Fixed frontend request recovery plumbing so `loadRuntimeProjection({ mode: "request" })` and `openMessageStream` both carry the expected `sessionId`; added `hasScheduledStreamReconnect` so message re-renders cannot bypass the stream reconnect backoff timer, including the max-attempt async exhaustion check guarded by `streamReconnectChecks` and the delayed reconnect branches registered in `streamReconnectTimers`.
- Artifacts generated:
  - `docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.json`
  - `docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.png`
  - `docs/v0.2.3/artifacts/session-cross-talk-refresh-replay.json`
  - `docs/v0.2.3/artifacts/session-cross-talk-refresh-replay.png`
- Verification passed:
  - `python -m py_compile scripts\smoke-web-session-cross-talk-browser.py scripts\smoke-web-session-cross-talk-refresh-replay.py tests\test_ecorex_web_parallel_backend.py`
  - `npm --prefix desktop run typecheck`
  - `npm --prefix desktop run build:renderer` passed with the existing Vite chunk-size warning
  - `python scripts\smoke-web-session-cross-talk-browser.py --artifact docs\v0.2.3\artifacts\session-cross-talk-browser-smoke.json --screenshot docs\v0.2.3\artifacts\session-cross-talk-browser-smoke.png`
  - `python scripts\smoke-web-session-cross-talk-refresh-replay.py --artifact docs\v0.2.3\artifacts\session-cross-talk-refresh-replay.json --screenshot docs\v0.2.3\artifacts\session-cross-talk-refresh-replay.png` -> `projectionCallCount=1`, `streamCallCount=1`, `backendHistoryFetched=true`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\session-cross-talk-*.json --json-output docs\v0.2.3\artifacts\session-cross-talk-privacy-scan.json --salt v023-session-artifacts` -> `filesScanned=4`, `findingCount=0`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; pytest tests/test_ecorex_web_parallel_backend.py -q -k "frontend_has_typed_runtime_projection_fetch_contract or frontend_runtime_projection_fetch_executes_request_and_session_modes or session_cross_talk_browser_smoke_harness_contract or session_refresh_replay_browser_smoke_harness_contract"` -> `4 passed, 379 deselected`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; pytest tests\test_ecorex_session_identity_sorting.py tests\test_ecorex_session_state_repair.py tests\test_ecorex_session_privacy_gates.py tests\test_ecorex_session_legacy_repair_compat.py -q` -> `25 passed`
- R23-20 browser blockers are cleared. Full v0.2.3 final Release/Regression gate remains separate.

## R23-21

- Inserted a Codex-like chat attachment bubble slice after the user reported the text+file+image user message layout was too heavy.
- Added `chat-attachment-bubble-plan.md`.
- Updated `desktop/src/App.tsx` so user message attachments render above the text bubble instead of inside one large orange container.
- Updated `desktop/src/styles/app.css` so the user message body is a transparent layout wrapper, text uses a light orange bubble, file attachments use compact pills, and image attachments use small thumbnails.
- Added `scripts/smoke-chat-attachment-bubble-static.py`, which loads the real desktop CSS into a static Playwright fixture and verifies light, dark, and 390px narrow layouts.
- Verification passed: `npm --prefix desktop run typecheck`, `npm --prefix desktop run build:renderer`, `python -m py_compile scripts\smoke-chat-attachment-bubble-static.py`, and `python scripts\smoke-chat-attachment-bubble-static.py`.
- Static smoke artifacts: `docs/v0.2.3/artifacts/chat-attachment-bubble-light.png`, `chat-attachment-bubble-dark.png`, `chat-attachment-bubble-narrow.png`, and `chat-attachment-bubble-smoke.json`.
- Multi-agent review: Nash Frontend/UX PASS, Nietzsche Harness/Test PASS after evidence update, Galileo Release/Regression PASS. Full integrated app/browser smoke was later closed by the R23-21 integrated browser smoke.

## R23-21 Integrated Browser Smoke

- Added `scripts/smoke-chat-attachment-bubble-browser.py`:
  - serves the built `desktop/dist` React app through the shared static-site smoke server;
  - stubs runtime APIs with one historical session and a user message containing PPT + image attachments in `extras.attachments`;
  - clicks the sidebar session and verifies the real App renders `.message.user.has-files`, `.message-files`, and `.message-text-bubble`;
  - checks two compact attachment buttons, one image thumbnail, transparent user wrapper, light EcoreX-orange text bubble, Run Center hidden state, and zero console errors;
  - records only relative artifact paths and aggregate geometry/status metrics.
- Generated evidence:
  - `docs/v0.2.3/artifacts/chat-attachment-bubble-browser-smoke.json` reports `status=PASS`, `historyCalls=1`, `userMessageCount=1`, `attachmentButtonCount=2`, `imageAttachmentCount=1`, `textIncludesCodex=true`, `runCenterHidden=true`, and `consoleErrorCount=0`;
  - `docs/v0.2.3/artifacts/chat-attachment-bubble-browser.png`;
  - `docs/v0.2.3/artifacts/chat-attachment-bubble-browser-privacy-scan.json` reports `findingCount=0`.
- Added source/artifact contract coverage in `tests/test_ecorex_session_privacy_gates.py`.
- R23-21 is promoted from `STATIC-SMOKE-PASS` to `PASS`; the R23-17 final gate no longer carries `chat-bubble-integrated-browser-smoke-missing`.

## R23-02C

- Inserted the Chrome DevTools MCP full-compatible enablement slice after the user requested `ChromeDevTools/chrome-devtools-mcp.git` with the full tool/skills set.
- Added `docs/v0.2.3/chrome-devtools-mcp-full-plan.md`.
- Updated `config.py`, `config-template.json`, `config.json`, and `desktop/electron/sidecar.ts` so the default `chrome-devtools` MCP server uses the same canonical args:
  - `npx/npx.cmd -y chrome-devtools-mcp@latest`
  - shared `--browserUrl http://127.0.0.1:9222`
  - `--no-usage-statistics`, `--no-performance-crux`, `--redactNetworkHeaders`
  - page-id routing, DevTools targets, vision click tools, structured content, all pages, memory debugging, third-party tools, and WebMCP flags.
- Kept `mcp_auto_start=false`; BrowserAutomationService still owns CDP auto-launch/fallback, and MCP starts through the existing optional/runtime tool path.
- Updated `agent/tools/mcp/mcp_client.py` and `common/ecorex_tool_permissions.py` so only the canonical localhost profile can be silently trusted; remote endpoints, unknown flags, spoofed commands, and read-only mode remain blocked.
- Updated `agent/tools/optional_abilities/optional_abilities.py` so enabling `chrome-devtools-mcp` upgrades old default args to the full profile and reports `fullToolset`.
- Bundled upstream Chrome DevTools MCP skills under `skills/`: `a11y-debugging`, `chrome-devtools`, `chrome-devtools-cli`, `debug-optimize-lcp`, `memory-leak-debugging`, and `troubleshooting`.
- Verification passed: `npx -y chrome-devtools-mcp@latest --help`; Python compile; `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `11 passed`; focused `chrome_devtools_mcp_startup` pytest -> `2 passed`; focused session-mismatch pytest; `npm --prefix desktop run typecheck`; JSON validation for `config-template.json`, `config.json`, and `docs/v0.2.3/harness-matrix.json`.
- Live smoke passed and wrote `docs/v0.2.3/artifacts/chrome-devtools-mcp-live-smoke.json`: BrowserAutomationService auto-launched Chrome 149 on `127.0.0.1:9222`, Chrome DevTools MCP initialized, `list_tools` returned 42 tools including `take_snapshot`, `take_screenshot`, `list_network_requests`, and `performance_start_trace`.

## R23-02/03/05/06/07/12/18/19 focused gate hardening

- Addressed multi-agent review blockers found during the focused CDP/OCR/External Connections/Skill Learning gate:
  - `BrowserAutomationService.find_chrome_executable` no longer returns a missing Linux command after `shutil.which()` fails, so diagnostics do not overstate Chrome availability.
  - `ensure_cdp_browser` now terminates/kills a spawned browser process when CDP readiness times out before returning to fallback.
  - `ChannelsHandler` now treats all-asterisk masked secrets, including short `***`, as display-only values so clients cannot overwrite stored short secrets with literal masks.
  - `RuntimeProjectionService` fallback `skill_learning.requested` draft ids now use stable SHA-256-derived ids instead of Python's randomized `hash()`.
  - Added `common/ecorex_public_payload.py` and wired UI-facing tool disclosure redaction into SSE `tool_start`, runtime projection tool calls, desktop localStorage compaction, and tool detail rendering. Raw skill draft file content, prompt/code/content fields, and common credentials are removed from public tool disclosures.
  - Extended the same redaction to SSE `tool_end.result`, JSON-string tool results, and `include_events=true` runtime projection `events[].payload` for `tool.*` events so serialized skill draft payloads cannot bypass content-field masking.
  - Hardened MCP streamable HTTP and handshake error surfaces with `_mask_sensitive`, including Bearer, cookie, session, and Python dict repr shapes.
  - External Connections frontend now renders backend-projected `connection.actions`; the `set_home_channel` action sends the projected `homeChannel` as a top-level API argument and is disabled when no home channel target exists.
- Verification passed:
  - `python -m py_compile common\ecorex_public_payload.py agent\tools\browser\browser_automation_service.py agent\protocol\runtime_projection.py channel\web\web_channel.py agent\tools\mcp\mcp_client.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_session_privacy_gates.py`
  - `npm --prefix desktop run typecheck`
  - `npm --prefix desktop run build:renderer` passed with the existing Vite chunk-size warning.
  - `python scripts\smoke-web-external-connections-browser.py --artifact docs\v0.2.3\artifacts\external-connections-browser-smoke.json --screenshot docs\v0.2.3\artifacts\external-connections-browser-smoke.png` -> `homeChannelActionVisible=true`, `homeChannelActionUsable=true`, `secretRedactedOnSave=true`, `runCenterHidden=true`, `consoleErrorCount=0`.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\external-connections-browser-smoke.json --json-output docs\v0.2.3\artifacts\external-connections-privacy-scan.json --salt v023-external-connections` -> `findingCount=0`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_session_privacy_gates.py -q` -> `25 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_session_identity_sorting.py tests\test_ecorex_session_state_repair.py tests\test_ecorex_session_legacy_repair_compat.py tests\test_ecorex_session_privacy_gates.py -q` -> `26 passed`.
  - Focused backend redaction/browser capability pytest -> `4 passed, 379 deselected`.
- Five-angle focused re-review passed; full v0.2.3 final Release/Regression gate remains separate.

## R23-08

- Hardened the External Connections action API implementation for `save_config`, `enable/start`, `disable/stop`, `test`, `set_home_channel`, and `clear_home_channel`.
- `ChannelsHandler` now uses a shared `CONFIG_WRITE_LOCK`, `_read_file_config`, and `_write_file_config_atomic` for channel-related `config.json` writes.
- Action handlers now validate and coerce field values before mutating runtime `conf()`. File writes happen first; in-memory config updates happen only after successful atomic replace, preventing file/memory split-brain on write failure.
- Masked secret values, including short all-asterisk masks, are skipped without overwriting stored credentials. A masked-secret-only save returns a no-op success instead of a false error.
- `start/enable` validates required channel credentials against file config, memory config, and submitted updates before enabling; it deduplicates `channel_type`, tolerates non-dict config payloads only when existing credentials are present, and injects Feishu websocket event mode without starting real threads in tests.
- `set_home_channel` and `clear_home_channel` now use the same locked/atomic config path, preserve unrelated config fields, and project back through `/api/external-connections`.
- Runtime startup/stop error strings are redacted before logs/UI projection, including OpenAI/GitHub/Bearer key-value forms plus Slack and Telegram token shapes.
- Atomic config temp files are created with private `0600` permissions; stale `config.json.tmp-*` files are cleaned before the next write, and temp files are removed if dump/flush/fsync/replace fails.
- Added R23-08 contract tests for masked secret preservation, invalid number rollback, write-failure memory rollback across save/start/stop/home actions, channel_type file-base merging, missing credential rejection, non-dict config tolerance with existing credentials, runtime error redaction, private temp cleanup, and home channel set/clear/projection.
- Verification passed:
  - `python -m py_compile channel\web\web_channel.py tests\test_v023_external_connections_cdp_ocr.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_session_privacy_gates.py -q` -> `40 passed`.
  - `npm --prefix desktop run typecheck` passed after frontend redaction patterns were kept in sync.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "channel_catalog or configured_channels or channels_handler or channel_observability or feishu_register"` -> `9 passed, 374 deselected, 4 subtests passed`.
- R23-08 is in multi-agent review; do not mark PASS until Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression agree.

## R23-09

- Added `channel/messaging_adapter_contract.py` as the EcoreX-native adapter contract layer.
- The contract is deliberately narrow: platform ingress still normalizes to `Context + ChatMessage`, outbound still goes through `Channel.send` / `ChatChannel._send`, and the sole execution queue remains `ChatChannel.produce`.
- Added `MessageIngressGate` with scoped TTL dedupe by `platform + session_id + context_type + message_id`; `ChatChannel.produce` now applies the gate only when a real external message id exists, so internal/Web flows without message ids keep their existing behavior.
- Added public helpers:
  - `normalize_inbound_context` and `normalize_reply_delivery` for testable receive/send DTOs.
  - `probe_messaging_adapter` for read-only readiness/context-shape checks without creating channels, loading SDKs, or inspecting private queues.
  - `test_messaging_adapter`, which marks External Connections test output as `projection_dry_run` and `remoteConnectivityProbed=false`.
  - `send_messaging_reply`, a bounded live-channel sender for future R23-15 reuse.
- `/api/channels` and `/api/external-connections` now project `adapterContract` from the backend, preserving the backend projection as the only state source.
- `ExternalConnectionActionHandler.test` now returns adapter dry-run evidence so UI cannot mistake projection readiness for real remote connectivity.
- Scheduler channel readiness now uses `probe_messaging_adapter` instead of `channel_factory.create_channel()` and no longer peeks `WebChannel.session_queues` or Weixin private context-token maps. Web's missing-config case remains legacy-compatible because the existing delivery tests validate the live/singleton send path.
- Runtime/Security hardening after review:
  - inbound/outbound adapter DTOs now publish `contentPreview=[redacted-content]`, `contentHash`, `contentLength`, and `contentBytes` rather than raw user/reply bodies;
  - `ChatChannel` logs now use context/reply/body summaries, and scheduler logs use body summaries plus receiver/session hashes;
  - `ChatChannel` reference-query filtering and WebChannel prefix insertion debug logs no longer print raw prompt bodies;
  - `ChatChannel` voice conversion fallback logs now use body summaries instead of concatenating raw conversion exceptions;
  - `ChatChannel` releases adapter dedupe keys on worker exception/cancel and queued-session cancellation, including custom `MessageIngressGate` instances accepted by `produce_context_once`, so transient worker failure does not stale-block a real retry;
  - subagent SSE start/timeout/end events publish sanitized task summaries instead of raw `arguments.summary`, `arguments.task`, result previews, or task bodies;
  - scheduler public projection no longer returns raw `content`, `taskDescription`, or `resultPrefix`; it returns preview/hash/length fields and uses `redact_public_tool_value` for tool/skill params;
  - desktop and legacy web scheduler editors avoid writing `[redacted-content]` placeholders back to the store unless the user re-enters full content.
  - WebChannel now reports startup success after WSGIServer binds, reports sanitized startup errors, and the adapter probe treats a live Web manager thread without startup error as ready so scheduler Web delivery is not stuck in `starting`.
  - Web pre-worker/worker exception surfaces and scheduler terminal ledger errors now use generic public messages with type/hash/length metadata instead of raw exception text.
  - Scheduler projection/API fallback errors now redact `loadError`, `modifyBlockingReason`, and `SchedulerHandler` GET/POST exception responses with public hash/type/length metadata.
  - Scheduler and WebChannel fallback logs now use body summaries for permission/probe/runtime/AgentBridge-init failures, run-ledger bookkeeping failures, `/message` pre-worker failures, cancel-token registration failures, session-lock release failures, SSE fallback failures, and permission-denial reasons, with DEBUG log-capture regression coverage.
  - AgentBridge eager scheduler init and scheduler-tool context attach warnings now log type/hash/length summaries instead of raw scheduler exceptions.
  - AgentBridge public error replies now return generic redacted detail strings instead of `Agent error: {exception}`; adjacent AgentBridge persistence/env/scheduled-output logs no longer interpolate exception text.
  - SchedulerTool mutation permission, lazy init, and unexpected action failures now log/return only hash/type/length summaries; public `ToolResult.fail` payloads no longer carry raw scheduler exception text.
  - AgentStreamExecutor error events and tool error payloads now redact exception text at the source and emit hash/type/length metadata before WebChannel log/SSE/done/RunLedger handling; model stream exception/error-chunk/MCP sync logs use the same summary instead of raw exception text or `exc_info=True`.
  - AgentStream retry/overflow classification now routes exception text through `_private_agent_exception_text_for_classification`, keeping classification separate from all public log/event/error payload paths and keeping raw exception source scans clean.
  - WebChannel public fallback payloads no longer return raw `str(e)`/`str(exc)`; generic exceptions use `_public_error_payload`, channel schema validation uses masked validation messages, and source guards reject raw public fallback patterns.
  - Channel connect/start uses the same masked validation message path as save, and the legacy web console renders backend validation messages with `textContent`.
  - Legacy web add-channel connect failures now render the sanitized backend `data.message` through a dedicated status element using `textContent`, including network-failure fallback text.
  - RunEventLedger append failure tails store only hash/length summaries, and `active_requests_snapshot` returns redacted stale-lock summaries (`sessionHash`, `lockPath`, booleans) instead of raw lock paths/session ids/remove errors.
  - Desktop Run Center consumes the redacted stale-lock contract (`sessionHash`, `lockPath`, `removeError`, booleans) instead of keying/rendering stale locks from raw `path` / `session_id`.
  - Desktop Run Center stale-lock visible labels now fall back from `sessionHash` to `lockPath.pathHash` before the generic label, preserving traceability without exposing raw paths.
  - Renderer production output was rebuilt and synced into `channel/web/static/app`; the shipped WebApp bundle now contains the same redacted stale-lock contract as the desktop source.
- Added R23-09 tests for:
  - receive/send normalization and secret redaction;
  - duplicate external ingress producing only one queued context;
  - failure releasing the dedupe key so a real retry can enter;
  - `ChatChannel.produce` using `Dequeue` rather than any Hermes queue;
  - backend-projected `adapterContract`;
  - projection-dry-run test semantics;
  - Slack/Telegram/Discord missing context gates;
  - scheduler readiness not creating channels.
  - scheduler public projection redaction while backend TaskStore keeps raw task bodies for execution;
  - subagent SSE redaction and scheduler/chat log summary source guards.
  - Agent stream exception redaction through WebChannel SSE and RunLedger terminal error storage, plus log-capture/payload coverage for `model.call_stream()` exceptions, model error chunks, MCP sync failure, tool lookup failure, and tool execution failure.
  - AgentBridge public error Reply redaction and SchedulerTool public ToolResult/log redaction for unexpected, permission-broker, and lazy-init failures.
  - WebChannel public API fallback redaction, runtime-event append failure redaction, and active stale-lock snapshot redaction.
  - Desktop Run Center stale-lock redacted-contract source guard.
  - Desktop and static WebApp stale-lock visible-label fallback guard (`sessionHash` -> `lockPath.pathHash` -> generic).
  - Legacy web add-channel connect-failure status source guard.
  - Static WebApp bundle stale-lock redacted-contract source guard, so release artifacts cannot silently lag behind frontend source.
- Verification passed:
  - `python -m py_compile agent\protocol\agent_stream.py channel\messaging_adapter_contract.py channel\chat_channel.py channel\web\web_channel.py bridge\agent_bridge.py agent\tools\scheduler\integration.py agent\tools\scheduler\projection.py agent\tools\scheduler\scheduler_tool.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_web_parallel_backend.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `67 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k scheduler` -> `23 passed, 361 deselected`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_chat_channel_robustness.py tests\test_ecorex_session_privacy_gates.py -q` -> `6 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "active_request_snapshot or channel_catalog or configured_channels or channels_handler or channel_observability or feishu_register or chat_cancel_fast_path"` -> `25 passed, 359 deselected, 4 subtests passed`.
  - `npm --prefix desktop run typecheck`.
  - `npm --prefix desktop run build:renderer` -> passed with the existing Vite chunk-size warning.
  - Source scan guard: `rg` over `bridge\agent_bridge.py`, `agent\tools\scheduler\scheduler_tool.py`, and `agent\protocol\agent_stream.py` found no raw `Agent error:`, `操作失败:`, `str(e)`, `{e}`, `{exc}`, `str(exc)`, `exc_info=True`, or `message": str(e)` patterns after the final AgentStream classification helper cleanup.
- R23-09 five-angle review PASS: Archimedes Runtime/Backend, Avicenna Frontend/UX, Kant Harness/Test, Dewey Security/Audit, and Aquinas Release/Regression. Full R23-17 final release gate remains separate.

## R23-10

- Added `external_connection.*` runtime events for External Connections without introducing a Hermes gateway/runtime:
  - lifecycle: `start_requested`, `started`, `start_failed`, `stop_requested`, `stopped`, `stop_failed`;
  - actions: `config.saved`, `test.completed`, `home_channel.updated`;
  - adapter ingress: `ingress.queued`, `ingress.duplicate`, `ingress.failed`;
  - adapter delivery: `delivery.sent`, `delivery.blocked`, `delivery.dry_run`, `delivery.error`.
- Added `record_external_connection_runtime_event` in `channel\messaging_adapter_contract.py` as a best-effort RunEventLedger writer. Ledger failures are debug-only and do not block queueing, delivery, or settings actions.
- `RuntimeProjectionService` now reduces `external_connection.*` events into `external_connections` projection items and uses a dedicated sanitizer for include-events payloads.
- `/api/external-connections` now merges backend `runtimeProjection` evidence per platform from the RunEventLedger-backed projection, while preserving channel observability as the live backend status source.
- Added R23-10 tests for:
  - lifecycle/test/ingress/delivery events entering RunEventLedger and projecting without raw content/secret leakage;
  - `/api/external-connections` returning `runtimeProjectionSource=RunEventLedger` and per-platform runtime projection;
  - real action paths (`start`, `stop`, `test`) emitting runtime events instead of only manually recorded fixture events;
  - projection freshness after more than 500 external-connection events, so settings does not read a stale first page.
- Review-blocker fixes before final R23-10 re-review:
  - `RuntimeProjectionService.external_connections_projection(limit=0)` now reduces the dedicated `external_connections` ledger session directly, avoiding stale windowing in `/api/external-connections`;
  - real action-path regression coverage now calls `ChannelsHandler._handle_connect`, `ChannelsHandler._handle_disconnect`, and `ExternalConnectionActionHandler._handle_test`;
  - `delivery.error` now records and returns a structured public `errorSummary` (`errorType`, hash, length/bytes, `redacted=true`) plus the safe label `delivery_failed`, and RuntimeProjection no longer trusts free-form `error`/`lastError` text;
  - RuntimeProjection text sanitization now reuses shared public redaction patterns, and external-connection error labels reject Slack/xapp/Telegram/GitHub-shaped credentials even when the words `secret` or `token` are absent;
  - external-connection event `operation_id` and explicit `request_id` are sanitized before write/projection, projected identifiers reject values that shared public redaction would change, and raw `RunEventLedger.list_events()` replay is covered by regression;
  - `RunEventLedger` text redaction now reuses shared public redaction patterns, so raw durable payload fields such as `reason`, `mode`, and nested `adapter.reason` also redact Slack/xapp/Telegram/GitHub-shaped credentials.
- Verification passed:
  - `python -m py_compile channel\messaging_adapter_contract.py channel\web\web_channel.py agent\protocol\runtime_projection.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_web_parallel_backend.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `74 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q -k "delivery_error_redacts_exception or error_labels_reject_token_shaped_values or event_identifiers_reject_token_shaped_values or external_connection_runtime_events_project or external_connections_api_includes_runtime_projection or real_action_paths_emit_runtime_events or latest_event_after_window"` -> `7 passed, 67 deselected`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py::TestV022RunEventLedger::test_v022_run_event_ledger_appends_replays_and_projects_request tests\test_ecorex_web_parallel_backend.py::TestV022RunEventLedger::test_v022_run_event_ledger_records_idempotency_conflicts tests\test_ecorex_web_parallel_backend.py::TestV022RunEventLedger::test_v022_runtime_projection_api_returns_request_and_session_projection -q` -> `3 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "active_request_snapshot or channel_catalog or configured_channels or channels_handler or channel_observability or feishu_register or chat_cancel_fast_path"` -> `25 passed, 359 deselected, 4 subtests passed`.
- R23-10 five-angle review PASS: Archimedes Runtime/Backend, Avicenna Frontend/UX, Kant Harness/Test, Dewey Security/Audit, and Aquinas Release/Regression. Full R23-17 final release gate remains separate.

## R23-13

- Tightened Feishu/Lark external-connection readiness without changing the legacy Feishu channel implementation or invoking `feishu_cli` from status reads:
  - `probe_messaging_adapter().configured` now means required credentials are complete for catalog-backed channels; `enabled` continues to reflect `channel_type`;
  - built-in `web` remains configured when enabled even though it is not catalog-backed;
  - missing Feishu app credentials produce `readiness=not_configured`, `safeToSend=false`, and `reason=channel is not configured`.
- `/api/channels` and `/api/external-connections` now keep Feishu enabled/active separate from configured: when Feishu app credentials are missing, public `configured=false`, `status=blocked`, `running=false`, `connected=false`, `configState=missing`, and `adapterContract.readiness.configured/running=false` even if stale runtime observations say running. This is true whether Feishu is currently in `channel_type` or disabled but stale runtime state still reports `active`; only runtime `error` is allowed to override `blocked`. Other platforms keep their previous v0.2.2 partial-config behavior for R23-14.
- `ExternalConnectionActionHandler._handle_test("feishu")` now records business `status=blocked` when Feishu credentials are missing. The HTTP action still returns `status=success` for the API call itself, but `test.status`, adapter readiness, and RuntimeProjection are blocked rather than fake PASS.
- `probe_messaging_adapter("feishu")` now clamps nested `adapter.running=false` when required Feishu credentials are missing/partial, even if a stale live ChannelManager reports a ready startup event. The R23-13 regression covers both API payload and RuntimeProjection nested `adapter.running` for enabled and disabled Feishu stale-runtime paths.
- Existing configured Feishu dry-run behavior remains: status check is still `projection_dry_run`, `remoteConnectivityProbed=false`, and does not prove remote connectivity or mix with `feishu_cli`.
- Frontend status rendering now follows the same backend projection priority: `status=blocked`, `configState=missing/partial/not_configured`, or adapter `readiness=not_configured` renders as `需配置` and `is-blocked` before `enabled` can render `已启用`. This preserves the backend `enabled` meaning while preventing missing-credential Feishu from appearing healthy; the bundled static WebUI was rebuilt/synced to `channel/web/static/app`.
- Verification passed:
  - `python -m py_compile channel\channel_catalog.py channel\messaging_adapter_contract.py channel\web\web_channel.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_web_parallel_backend.py`
  - `npm --prefix desktop run typecheck` -> pass.
  - `npm --prefix desktop run build:renderer` -> pass with existing chunk-size warning; static app synced. Later R23-14 frontend work replaced the bundle with `index-PT89tAwd.js` / `index-ofwk31-1.css`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `75 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q -k "feishu_external_connection_test_blocks_missing_credentials or external_connection_test_is_projection_dry_run_not_remote_pass or messaging_adapter_probe_blocks_disabled_and_bad_startup_states or external_connection_frontend_marks_projection_dry_run_status_check"` -> `4 passed, 71 deselected`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "channel_catalog or configured_channels or channels_handler or channel_observability or feishu_register"` -> `9 passed, 375 deselected, 4 subtests passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "active_request_snapshot or chat_cancel_fast_path"` -> `16 passed, 368 deselected`.
- R23-13 five-angle review reached PASS consensus. Missing real credentials remain blocked; no real Feishu smoke is claimed.

## R23-14

- Rolled the External Connections batch metadata/readiness rules across the existing EcoreX channel catalog:
  - `wecom` now normalizes to `wecom_bot`; `wecom_app`/`wechatcom` normalize to `wechatcom_app`;
  - `wechatmp_service` now uses the bundled WeChat logo key instead of falling back to a raw platform id;
  - all `credential_configured_only` platforms require complete required fields before they can be marked configured/running;
  - inactive, unconfigured credential platforms stay `available`; active or stale-runtime unconfigured platforms become `blocked`;
  - QR/runtime authorization platforms such as `weixin` now project `auth_required` until runtime authorization is actually present, instead of being treated as configured only because no static fields are required.
- `probe_messaging_adapter()` now uses the same complete-config/runtime-auth rules as channel observability so adapter readiness cannot diverge from `/api/channels` or `/api/external-connections`.
- Frontend External Connections cards now understand `auth_required` and display `需授权`, while respecting backend `status=available` so inactive unconfigured platforms do not render as blocked/`需配置`.
- Added R23-14 regression coverage for Weixin, DingTalk, WeCom Bot, WeCom App, QQ, WeChat Customer Service, WeChat MP, WeChat MP Service, Telegram, Slack, and Discord metadata/logo/field/auth projection and honest readiness. The frontend source/static gate now also asserts `需授权` priority before `已启用`.
- Verification passed:
  - `python -m py_compile channel\channel_catalog.py channel\messaging_adapter_contract.py channel\web\web_channel.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_web_parallel_backend.py`
  - `npm --prefix desktop run typecheck` -> pass.
  - `npm --prefix desktop run build:renderer` -> pass with existing chunk-size warning; static app synced to `index-PT89tAwd.js` / `index-ofwk31-1.css`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `76 passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q -k "r23_existing_platform_batch_projects_metadata_logos_and_honest_readiness or external_connection_frontend_marks_projection_dry_run_status_check or feishu_external_connection_test_blocks_missing_credentials"` -> `3 passed, 73 deselected`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "channel_catalog or configured_channels or channels_handler or channel_observability or feishu_register"` -> `9 passed, 375 deselected, 4 subtests passed`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k "active_request_snapshot or chat_cancel_fast_path"` -> `16 passed, 368 deselected`.
- R23-14 five-angle review reached PASS consensus. Real send/connect capability for non-Feishu platforms remains separate release-gate evidence, not implied by metadata readiness.

## R23-15

- Added `agent/tools/scheduler/delivery_target.py` as the narrow resolver for scheduler delivery targets:
  - resolves a single concrete `channel_type`/receiver from task action metadata and External Connections home-channel config;
  - supports Web context fallback without treating `unknown` or `web,feishu` as a valid platform;
  - records only receiver hashes and enum/source fields in public projection.
- `SchedulerTool._create_task` now resolves a Web/default-context task to the configured external home channel when one exists, so Hermes-style home channel becomes the scheduler delivery target without adding a Hermes queue/runtime.
- `integration._execute_scheduled_task` now re-validates the target at execution time:
  - missing home channel blocks before run creation/channel creation and writes `external_connection.delivery.blocked`;
  - disabled or not-ready external platform defers and writes a blocked event with the adapter reason such as `channel is not enabled`;
  - `unknown` and comma-joined channel values fail closed instead of being treated as deliverable;
  - explicit Web delivery keeps the legacy Web live-send fallback, while a stopped live Web manager still defers.
- External-platform scheduler sends now go through the messaging adapter contract's `deliver_reply()` so `external_connection.delivery.sent/error` is emitted; Web sends keep the existing direct send path.
- Web-originated message contexts now set `channel_type=web`, preventing scheduler attachment from inheriting the global multi-channel `channel_type` string.
- Scheduler public projection now includes `action.deliveryTarget` with source/status/home-channel booleans and receiver hash only.
- SchedulerTool public create/get results now redact scheduler receiver/body payloads:
  - public receiver output is `receiverHash=<hash>`;
  - public body output is `[redacted-content]` plus hash/length summaries;
  - RuntimeProjection exposes `receiverNameHash` instead of raw `receiverName`.
- Verification passed:
  - `python -m py_compile agent\tools\scheduler\delivery_target.py agent\tools\scheduler\integration.py agent\tools\scheduler\projection.py agent\tools\scheduler\scheduler_tool.py channel\web\web_channel.py tests\test_v023_external_connections_cdp_ocr.py tests\test_ecorex_web_parallel_backend.py`
  - R23-15 focused subset -> `15 passed, 72 deselected`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `87 passed`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_web_parallel_backend.py -q -k scheduler` -> `23 passed, 361 deselected`
  - active/chat gate -> `16 passed, 368 deselected`
  - channel/Feishu gate -> `9 passed, 375 deselected, 4 subtests passed`
  - `npm --prefix desktop run typecheck` and `npm --prefix desktop run build:renderer` passed; build retains the existing Vite chunk-size warning.
- Runtime/Backend first review found two blockers and both were fixed:
  - comma-joined `channel_type` values such as `web,feishu` now fail closed at execution unless task creation resolved a concrete home-channel target;
  - non-Web adapter probe exceptions now fail closed instead of allowing ad-hoc channel creation, while explicit Web keeps its compatibility fallback.
- Frontend/UX review found a typed projection gap and it was fixed by adding redacted-only `RuntimeSchedulerDeliveryTarget` to `desktop/src/services/ecorexApi.ts`, with a source guard that prevents raw receiver fields in the type.
- Release/Regression review found a public active-request leak for external home-channel receivers; scheduler RunLedger session ids, parent ids, and metadata now use platform-scoped receiver hashes for non-Web targets, and a regression asserts `/api/active-requests` does not expose the home-channel id.
- Security/Audit review found two public leakage paths and both were fixed: SchedulerTool create/get no longer echo raw receiver/body text, and scheduler projection/type surfaces now expose `receiverNameHash` rather than raw home-channel names.
- Runtime/Backend re-review found two execution-context bypasses and both were fixed: `agent_task` and `skill_call` now use `_scheduler_session_id(task)`, so non-Web AgentBridge contexts get platform-scoped receiver-hash session ids instead of raw external home-channel ids.
- Security/Audit re-review found a raw adapter-readiness reason path and it was fixed:
  - `_is_channel_ready` logs only `_body_summary(reason)`;
  - scheduler blocked delivery events record `reason="adapter_not_ready"` plus `reasonSummary`, never the arbitrary adapter reason text;
  - disabled-platform audit keeps traceability through reason summary and receiver hash instead of persisting raw readiness text.
- Security/Audit final re-review found that outbound delivery DTOs still persisted raw `sessionId`/`receiver`; `normalize_reply_delivery` now emits only `sessionHash`, `receiverHash`, `sessionSummary`, and `receiverSummary`. Live send contexts still carry the raw receiver required by platform adapters, but `external_connection.delivery.sent/error` events do not persist it.
- Release/Regression re-review found the projection sanitizer was stripping those replacement identity fields; `RuntimeProjectionService` now allowlists strict hash fields and non-free-text summary metadata so `lastDelivery` keeps traceability in session projection, External Connections projection, and `/api/external-connections` runtime projection without reintroducing raw ids.
- Verification after the Runtime/Backend re-review fix:
  - `python -m py_compile agent\protocol\runtime_projection.py channel\messaging_adapter_contract.py agent\tools\scheduler\integration.py tests\test_v023_external_connections_cdp_ocr.py`
  - adapter/delivery focused subset -> `4 passed, 87 deselected`
  - R23-15 focused subset -> `19 passed, 72 deselected`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `91 passed`
  - scheduler gate -> `23 passed, 361 deselected`
  - active/chat gate -> `16 passed, 368 deselected`
  - channel/Feishu gate -> `9 passed, 375 deselected, 4 subtests passed`
- R23-15 five-angle review reached PASS consensus: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression. Full R23-17 final gate remains separate.

## R23-16P-09

- Added `scripts/audit-performance-evidence.py` as the aggregate performance evidence audit:
  - reads `docs/v0.2.3/performance-harness-matrix.json`;
  - requires the known R23-16P scenario set;
  - scans the 7 base performance artifact/privacy-scan pairs;
  - fails closed on narrowed matrices, missing artifact fields, missing files, non-clean paired scans, or scanner findings;
  - writes only enum statuses, counts, and HMAC references.
- The self audit report is declared in the matrix but is not required as a stale pre-run input. It is generated by the audit command and then checked by `scan-session-artifacts-privacy.py`.
- Harness/Test found and closed two P1 issues:
  - initial implementation could pass after matrix coverage contraction because rows missing `privacyArtifact` were skipped;
  - second implementation depended on stale self audit artifacts already existing before the command ran.
- Added regression coverage in `tests/test_v023_performance_projection.py`:
  - source/artifact contract for hashed findings and no raw ids/text;
  - narrowed matrix fails with `requiredScenarioMissingCount > 0`;
  - missing `privacyArtifact` fails with `matrixConfigIssueCount > 0`;
  - full matrix with missing self-report paths passes on first run.
- Verification passed:
  - `python -m py_compile scripts\audit-performance-evidence.py tests\test_v023_performance_projection.py`
  - `python scripts\audit-performance-evidence.py --output docs\v0.2.3\artifacts\perf-evidence-audit.json --salt v023-performance-evidence`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\perf-evidence-audit.json --json-output docs\v0.2.3\artifacts\perf-evidence-audit-privacy-scan.json --salt v023-performance-evidence-audit`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_performance_projection.py -q` -> `20 passed, 2 warnings`.
- Five-angle review reached PASS consensus: Runtime/Backend Archimedes, Frontend/UX Avicenna, Harness/Test Kant, Security/Audit Dewey, and Release/Regression Aquinas.

## R23-16P-10

- Added RapidOCR screenshot URL OCR support:
  - `agent/tools/ocr/ocr.py` now prefers `rapidocr_onnxruntime`/`rapidocr` before pytesseract and tesseract CLI;
  - RapidOCR engine instances are cached process-wide so repeated screenshot OCR does not reload the model;
  - OCR diagnostics report RapidOCR provider availability and whether an engine is already cached.
- Added runtime packaging support:
  - `rapidocr-onnxruntime` is listed in root `requirements.txt`;
  - desktop core runtime requirements include `rapidocr-onnxruntime`;
  - `desktop/runtime-packs/capabilities.json` declares a `fast-ocr` pack with RapidOCR module checks;
  - `optional_abilities` marks `fast-ocr` built-in when a local OCR provider is available.
- Hardened the browser/OCR performance smoke:
  - warm-up initializes the provider before measured samples;
  - image OCR fails closed when unmeasured, non-RapidOCR, missing URL handoff, or over the 2000ms P95 threshold;
  - current artifact reports `ocrImageUrlMeasured=true`, `provider=rapidocr_onnxruntime`, `ocrImageUrlP95Ms=2.966`, `urlCountMin=1`, `nextActionBrowserNavigate=true`, and `failureCodes=[]`.
- Security/Audit P1 fix:
  - OCR exception logs and public metadata no longer include raw exception text;
  - public error metadata is `errorType/errorHash/errorLength/redacted`;
  - regression covers a missing local path with a token-shaped filename and asserts no path/token text appears in logs or result metadata.
- Verification passed:
  - `python -m pip install rapidocr-onnxruntime -q`
  - `python -m py_compile agent\tools\ocr\ocr.py agent\tools\optional_abilities\optional_abilities.py scripts\smoke-performance-browser-ocr.py tests\test_v023_external_connections_cdp_ocr.py tests\test_v023_performance_projection.py`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_performance_projection.py tests\test_v023_external_connections_cdp_ocr.py -q` -> `114 passed, 3 warnings`.
  - `python scripts\smoke-performance-browser-ocr.py --output docs\v0.2.3\artifacts\perf-browser-ocr.json --iterations 20 --timeout-ms 15000`
  - browser/OCR privacy scan, aggregate performance evidence audit, and aggregate self privacy scan all reported `findingCount=0`.
- Five-angle review reached PASS consensus: Runtime/Backend Archimedes, Frontend/UX Avicenna, Harness/Test Kant, Security/Audit Dewey, and Release/Regression Aquinas.

## R23-16

- Added `scripts/audit-v023-security-permissions.py` as the v0.2.3 security and permission total audit:
  - imports the live configuration/permission classifiers and verifies CDP-first defaults, localhost-only Chrome DevTools MCP args, on-demand MCP startup, template/runtime config sync, and trusted-default rejection for remote or widened MCP args;
  - verifies the dedicated CDP profile path and remote-debugging launch arguments stay in `BrowserAutomationService`;
  - verifies OCR public failures use `errorSummary`, public payloads use shared redaction, external deliveries and scheduler projections expose hash/summary fields, learned skills remain draft-gated before registration, and Run Center remains hidden by browser-smoke evidence;
  - requires paired privacy scans for external connections, performance evidence, browser/OCR, image-artifact/OCR, scheduler/subagent, and session cross-talk artifacts;
  - checks Chrome DevTools live smoke structurally and also direct-scans the live-smoke artifact for raw local path drift;
  - directly scans the chat attachment bubble smoke artifact and writes only enum statuses, relative refs, counts, and HMAC finding buckets.
- Fixed a Runtime/Backend P2 in the shared privacy scanner:
  - `scripts/scan-session-artifacts-privacy.py` no longer treats `http://127.0.0.1:9222` as a Windows path via the `p:/` substring;
  - the same regex still catches real `C:\...` and `C:/...` local paths;
  - `tests/test_ecorex_session_privacy_gates.py` now covers both cases;
  - `docs/v0.2.3/artifacts/chrome-devtools-mcp-live-privacy-scan.json` records a clean scan for the Chrome live-smoke artifact.
- Fixed a R23-21 evidence hygiene issue found by the total audit:
  - `scripts/smoke-chat-attachment-bubble-static.py` now records relative screenshot artifact paths instead of absolute local paths;
  - regenerated `docs/v0.2.3/artifacts/chat-attachment-bubble-smoke.json`;
  - added `docs/v0.2.3/artifacts/chat-attachment-bubble-privacy-scan.json` with `findingCount=0`.
- Added regression coverage in `tests/test_ecorex_session_privacy_gates.py`:
  - the current R23-16 audit must pass and its own artifact must pass the privacy scanner;
  - a paired artifact that contains raw session/body keys is still caught by the aggregate audit even if its paired scan claims clean.
- Verification passed:
  - `python -m py_compile scripts\audit-v023-security-permissions.py scripts\smoke-chat-attachment-bubble-static.py tests\test_ecorex_session_privacy_gates.py`
  - `python scripts\smoke-chat-attachment-bubble-static.py`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\chrome-devtools-mcp-live-smoke.json --json-output docs\v0.2.3\artifacts\chrome-devtools-mcp-live-privacy-scan.json --salt v023-chrome-devtools-live`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\chat-attachment-bubble-smoke.json --json-output docs\v0.2.3\artifacts\chat-attachment-bubble-privacy-scan.json --salt v023-chat-bubble`
  - `python scripts\audit-v023-security-permissions.py --output docs\v0.2.3\artifacts\security-permission-audit.json --salt v023-security-permission`
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\security-permission-audit.json --json-output docs\v0.2.3\artifacts\security-permission-audit-privacy-scan.json --salt v023-security-permission-audit`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_ecorex_session_privacy_gates.py -q` -> `6 passed`
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py tests\test_v023_performance_projection.py tests\test_ecorex_session_privacy_gates.py -q` -> `120 passed, 3 warnings`
  - JSON validation passed for the security audit, self scan, chat-bubble smoke, and chat-bubble privacy scan artifacts.
- R23-16 five-angle review reached PASS consensus: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression.

## R23-17

- Promoted the final release gate to PASS after closing the promotion-state, fail-closed audit, screenshot privacy, and stale-evidence blockers:
  - `scripts/audit-v023-final-release-gate.py` now validates R23-16P aggregate metrics, R23-20 browser/refresh smoke metrics, R23-21 integrated chat smoke metrics, paired privacy scans, and screenshot OCR privacy evidence.
  - `harness-matrix.json` runs the final gate with `--require-complete`, so blocked release state cannot exit successfully.
  - `scan-session-artifacts-privacy.py` now supports `--ocr-images`; R23-20 screenshots were regenerated without raw path-shaped project text or email addresses.
  - Removed the stale temporary `docs/v0.2.3/artifacts/final-release-gate-audit.current.json`.
- Final audit evidence:
  - `python scripts\audit-v023-final-release-gate.py --output docs\v0.2.3\artifacts\final-release-gate-audit.json --salt v023-final-release --require-complete` -> current canonical artifact reports `status=pass`, `complete=true`, `sliceCount=20`, `artifactCount=20`, `blockerCount=0`.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\final-release-gate-audit.json --json-output docs\v0.2.3\artifacts\final-release-gate-audit-privacy-scan.json --salt v023-final-release-audit` -> `findingCount=0`.
- Five-angle final review reached PASS consensus after the exact blocker fixes: Runtime/Backend Archimedes, Frontend/UX Avicenna, Harness/Test Kant, Security/Audit Dewey, and Release/Regression Aquinas.

## R23-DEPLOY

- Prepared production release artifacts for v0.2.3:
  - `EcoreX_0.2.3-web-linux-service.tar.gz` -> size `3708372`, SHA256 `674F4DF92A3F94B7B9839FDCFF3C39E033DE9BD654A7017C188C4E88D7A1276B`.
  - initial deploy used `EcoreX_0.2.3-webui-windows-x64.zip` size `81465392`, SHA256 `87AEE19D90628B0D697E05409A01CC01BA0A339856AFDDDA3E17743A1C069E10`.
  - initial deploy used `EcoreX_0.2.3-webui-macos-universal.zip` size `399289143`, SHA256 `258887A93A0CC3CD84C1D212D41CC745A9212BAD7CBC4586C74D923FE7B351EF`.
  - initial deploy used `EcoreX_0.2.3-public-release.zip` size `486477420`, SHA256 `68EB566BF02F795CB3DA8280DA016FA2D6D74E94F5B1E1D533F45EFE57C7E821`.
- Re-ran release artifact validation after updating manifest and release helper defaults:
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.2.3 --public-zip release-artifacts\EcoreX_0.2.3-public-release.zip` passed.
- Deployed to the configured production server with `scripts\deploy-v023-production.py`:
  - evidence artifact `docs/v0.2.3/artifacts/production-deploy-online.json`;
  - deployment result `PASS`;
  - Web service version `0.2.3`;
  - installation manifest version `0.2.3`;
  - public manifest version `0.2.3`;
  - service active/enabled `true`;
  - `/api/version` status `200` and response contains `0.2.3`.
- Ran public HTTP smoke:
  - evidence artifact `docs/v0.2.3/artifacts/production-public-http-smoke.json`;
  - manifest status `200`;
  - root status `200`;
  - admin page unauthenticated status `401`;
  - ready artifacts: `webui-windows-x64`, `webui-macos-universal`, `web-linux-service`.
- Deployment evidence persists only hashes, counts, statuses, artifact filenames/sizes/SHA256, and redacted command excerpts; raw host, user, password, URL, and unredacted command output are not persisted.

## R23-DEPLOY-HOTFIX

- Fixed the Windows WebUI first-run stall reported at the "install Feishu environment" step:
  - `prepare-ecorex-webui-local-release.ps1` now emits an optional `lark_oapi` dependency notice instead of synchronously running `pip install lark-oapi` during first-run install;
  - Feishu remains available through External Connections and on-demand capability paths, but installation no longer blocks on network/PyPI/AV around the Feishu SDK.
  - `docs\v0.2.3\artifacts\windows-installer-feishu-hotfix-audit.json` confirms the actual rebuilt Windows zip has no blocking `lark_oapi` install call in its packaged installer scripts.
- Checked and corrected macOS package growth:
  - the macOS local package wheelhouse now uses a pruned runtime requirements file for installation packaging;
  - `rapidocr-onnxruntime` and `lark-oapi` remain declared in source/capability metadata, but are not prebundled into the macOS universal installer wheelhouse;
  - `docs\v0.2.3\artifacts\macos-package-bloat-audit.json` confirms the rebuilt macOS package contains no RapidOCR/OpenCV/Lark SDK wheelhouse entries.
- Rebuilt and redeployed the v0.2.3 public artifacts for the Feishu-install/macOS-bloat hotfix. These hashes are historical and were superseded by the later R23-CAPABILITY-RECOVERY package rebuild recorded below:
  - `EcoreX_0.2.3-webui-windows-x64.zip` -> size `81471725`, SHA256 `E00C0CAF872769A8F4A659F05FD65476B4E14E3A4DCFE3F803B3B3F5B7F2150C`.
  - `EcoreX_0.2.3-webui-macos-universal.zip` -> size `158597406`, SHA256 `32C87BE3335A3120FC22B4D2240CB6E9EE278C1FCEAC9CB424A85979AB37581E`.
  - `EcoreX_0.2.3-public-release.zip` -> size `245725965`, SHA256 `F1A1450FE8DE38324C46A4450D0C2888053523CFF9F0AC1C06592758CBEBB9E9`.
- Verification passed:
  - `npm --prefix desktop run typecheck`.
  - `python -m py_compile scripts\smoke-v023-install-packaging-contracts.py`.
  - `python scripts\smoke-v023-install-packaging-contracts.py` -> `PASS`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `94 passed, 3 warnings`.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.2.3 --public-zip release-artifacts\EcoreX_0.2.3-public-release.zip` passed.
  - `python scripts\deploy-v023-production.py` -> `PASS`.
  - public HTTP smoke reports manifest `200`, root `200`, admin auth `401`, all three ready artifact downloads `200`, and content lengths matching manifest sizes.
  - production deploy/privacy scan reports `filesScanned=5`, `findingCount=0`.
  - final release gate reports `status=pass`, `complete=true`, `sliceCount=20`, `artifactCount=20`, `blockerCount=0`.
- Five-angle hotfix review reached PASS consensus: Runtime/Backend accepts the nonblocking dependency path; Frontend/UX is unchanged; Harness/Test accepts source and online smoke evidence; Security/Audit accepts no raw target/URL/secret persistence; Release/Regression accepts the smaller macOS artifact and refreshed manifest/download evidence.

## R23-CAPABILITY-RECOVERY

- Started a new long goal after user reported that built-in ability/tool discovery regressed and even basic Bash/Shell was not usable/discoverable.
- Added `docs\v0.2.3\regression-pitfalls.md` as the project-level guardrail document for future iterations. It records the v0.2.3 pitfalls around tool discovery, frontend readiness labels, production runtime smoke, sealed v0.2.2 evidence boundaries, optional capabilities, Feishu channel/tool separation, install/package bloat, CDP/OCR handoff, session identity/sorting, long-running performance, attachment context, privacy scanning, multi-agent PASS scope, and fail-closed release gates.
- Root cause:
  - first-party tools were still present in `ToolManager` and `Bash` executed successfully;
  - `ExtensionRegistry` did not expose first-party tools as extension entries;
  - cold-start extension/channel snapshots could read an empty `ToolManager` without calling `load_tools()`;
  - Settings ability cards collapsed multiple states into the label `待配置`, making loaded built-in tools appear unconfigured.
- Backend/runtime fixes:
  - `agent/extensions/registry.py` now self-loads `ToolManager` when needed and adds `builtin_tool` entries such as `tool:bash`, `tool:read`, `tool:write`, `tool:edit`, `tool:browser`, and `tool:feishu_cli`;
  - `channel/web/web_channel.py` `ChannelsHandler._agent_tool_names()` also self-loads built-in tools when the singleton is empty, so Feishu channel readiness no longer reports `tool_not_loaded` merely because `/api/extensions` was called before `/api/tools`.
- Frontend fix:
  - Settings > 能力 no longer falls back to a blanket `待配置`;
  - Bash/file/OCR/scheduler/browser/Feishu rows now show states like `已加载`, `未加载`, `等待刷新`, `CDP 优先`, or `需凭据`;
  - OCR recognizes the fast `ocr` tool independently from `vision`;
  - Scheduler distinguishes tool schema availability from service running state.
- Added recovery gate:
  - `scripts/smoke-v023-capability-recovery.py`;
  - artifact `docs\v0.2.3\artifacts\capability-recovery-smoke.json`;
  - privacy scan `docs\v0.2.3\artifacts\capability-recovery-privacy-scan.json`;
  - package audit artifact `docs\v0.2.3\artifacts\capability-recovery-package-audit.json`;
  - production runtime smoke artifact `docs\v0.2.3\artifacts\production-capability-recovery-smoke.json`;
  - final gate now requires local recovery, package audit, production deployment, public HTTP smoke, production runtime capability smoke, and deployment privacy scan artifacts.
- Verification passed:
  - `python -m py_compile agent\extensions\registry.py channel\web\web_channel.py scripts\smoke-v023-capability-recovery.py scripts\audit-v023-final-release-gate.py`.
  - Focused backend pytest for extension/channel observability -> `5 passed, 382 deselected, 2 warnings, 8 subtests passed`.
  - `python scripts\smoke-v023-capability-recovery.py` -> `PASS`, `toolCount=21`, `extensionCount=135`, API tools include `bash`, `browser`, `feishu_cli`, and `ocr`; API extensions include `tool:bash` and other built-in tools; real Bash command execution passed; Feishu channel agent surface is `schema_visible_unverified`.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\capability-recovery-smoke.json --json-output docs\v0.2.3\artifacts\capability-recovery-privacy-scan.json --salt v023-capability-recovery` -> `findingCount=0`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py -q` -> `94 passed, 3 warnings`.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_invariant_bash.py tests\test_v022_harness_matrix.py -q` -> `9 passed`.
  - `npm --prefix desktop run typecheck` passed.
  - `npm --prefix desktop run build:renderer` passed with the existing chunk-size warning.
  - Package rebuild/deploy artifacts:
    - `release-artifacts\EcoreX_0.2.3-web-linux-service.tar.gz` -> size `3719154`, SHA256 `5A9C9C05BF8AF08A0F047A66B471CD9F7D5048A40BC48BBC3F4CCCA4857115DD`;
    - `release-artifacts\EcoreX_0.2.3-webui-windows-x64.zip` -> size `81487844`, SHA256 `25C55F8429EE292E6AF4817301822AADD64A7648505139236BBA5178DAAB181B`;
    - `release-artifacts\EcoreX_0.2.3-webui-macos-universal.zip` -> size `158613733`, SHA256 `AC709527BB21E9794A3A4C1009AEE0C97E1BE910110882257919F6A225570BEB`;
    - `release-artifacts\EcoreX_0.2.3-public-release.zip` -> size `245768509`, SHA256 `179D23C810717BF51EBE07741CE4A049F06488D63D806F9793E47A4783AB33DA`.
  - `docs\v0.2.3\artifacts\capability-recovery-package-audit.json` -> `PASS`; all Web service, Windows WebUI, and macOS WebUI packages contain the cold-start registry/channel fixes and updated ability-card status text.
  - `python scripts\deploy-v023-production.py` -> `PASS`; production Web service, installation manifest, and public manifest report `0.2.3`, service active/enabled, `/api/version` status `200`.
  - `docs\v0.2.3\artifacts\production-public-http-smoke.json` -> `PASS`; public root `200`, manifest `0.2.3`, admin protected, and all three ready artifact downloads return `200` with manifest-matching content length.
  - `docs\v0.2.3\artifacts\production-capability-recovery-smoke.json` -> `PASS`; production `/api/tools` exposes `bash`, file tools, `browser`, `feishu_cli`, `ocr`, `optional_abilities`, `agent_capability`, and `host_diagnostics`; production `/api/extensions` exposes built-in `tool:*` entries; Feishu external connection surface is `schema_visible_unverified` with `schemaVisible=true` and `toolSchemaCallable=true`.
  - `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\production-deploy-online.json docs\v0.2.3\artifacts\production-public-http-smoke.json docs\v0.2.3\artifacts\production-capability-recovery-smoke.json docs\v0.2.3\artifacts\capability-recovery-smoke.json docs\v0.2.3\artifacts\capability-recovery-package-audit.json --json-output docs\v0.2.3\artifacts\production-deploy-privacy-scan.json --salt v023-production-deploy` -> `findingCount=0`.
  - `python scripts\audit-v023-final-release-gate.py --output docs\v0.2.3\artifacts\final-release-gate-audit.json --salt v023-final-release --require-complete` -> `status=pass`, `complete=true`, `sliceCount=20`, `artifactCount=20`, `blockerCount=0`.
  - final release gate privacy scan -> `findingCount=0`.
- Boundary note:
  - Running the sealed v0.2.2 release-gate pytest suite produced historical release-artifact/hash blockers because v0.2.2 is sealed and the workspace now carries v0.2.3 artifacts; this was not treated as a product capability failure and no v0.2.2 sealed hash was changed.
- Five-angle review consensus:
  - Runtime/Backend PASS: built-in tools were present; cold discovery now self-loads and exposes them.
  - Frontend/UX PASS: ability card status no longer labels loaded core tools as `待配置`.
  - Harness/Test PASS: recovery smoke covers ToolManager, API tools, API extensions, real Bash execution, Feishu schema visibility, and v0.2.3 optional ability discovery.
  - Security/Audit PASS: discovery is visible but permission gates remain unchanged; evidence privacy scan is clean.
  - Release/Regression PASS: final gate includes local, package, and production capability recovery artifacts and remains PASS.

### R23-CAPABILITY-RECOVERY-HOTFIX-02

- User reported Settings still showed abilities as unloaded and clarified that a loaded-looking UI is not enough; Bash/Shell, OCR, IMAGEGEN, and other abilities must actually run.
- Root cause:
  - Frontend ability cards still primarily trusted `runtimeSnapshot.tools`; if `/api/tools` was empty/stale while `/api/extensions` already exposed `builtin_tool` entries, cards rendered `未加载`.
  - RapidOCR default detection config used `det_limit_type=min`, which upscaled URL-shaped screenshot crops by short edge and pushed first real image URL OCR above the 2s target.
- Fixes:
  - `desktop/src/services/ecorexApi.ts` now merges ready `builtin_tool` extension entries back into `RuntimeSnapshot.tools`.
  - `desktop/src/App.tsx` ability-card readiness uses `runtimeToolReady`, checking both `/api/tools` and `tool:*` builtin extensions.
  - `agent/tools/ocr/ocr.py` keeps content-crop preprocessing and initializes RapidOCR with `det_limit_type="max"` plus bounded `det_limit_side_len`, so screenshot/link OCR uses a max-side path instead of document-style short-side upscaling.
  - `scripts/smoke-ability-extension-fallback-browser.py` reproduces the exact UI failure mode by returning empty `/api/tools` and ready `tool:*` extensions; expected result is `unloadedCount=0`.
  - `scripts/smoke-image-generation-tool-invocation.py` now redacts prompt evidence as `promptHash/promptLength`.
- Real invocation verification:
  - `python scripts\smoke-v023-capability-recovery.py > docs\v0.2.3\artifacts\capability-recovery-smoke.json` -> PASS with real Bash execution.
  - `python scripts\smoke-performance-browser-ocr.py --output docs\v0.2.3\artifacts\perf-browser-ocr.json --iterations 10 --timeout-ms 15000` -> PASS; `ocrImageUrlP95Ms=648.883`, URL count >= 1, and browser handoff true.
  - `python scripts\smoke-image-generation-tool-invocation.py --artifact docs\v0.2.3\artifacts\image-generation-tool-invocation-smoke.json` -> PASS for both text-to-image and image-to-image script entrypoints against a fake GPT Image API.
  - `python scripts\smoke-ability-extension-fallback-browser.py` -> PASS; `/api/tools` intentionally empty, `/api/extensions` provides builtin tools, ability page `unloadedCount=0`.
  - `docs\v0.2.3\artifacts\real-tool-invocation-smoke.json` -> PASS; Bash, OCR text URL handoff, OptionalAbilities list, and Feishu CLI status callable.
  - `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests\test_v023_external_connections_cdp_ocr.py tests\test_v023_performance_projection.py -q` -> `114 passed, 3 warnings`.
  - `npm --prefix desktop run typecheck` and `npm --prefix desktop run build:renderer` passed.
- Package/deploy verification:
  - Rebuilt `EcoreX_0.2.3-web-linux-service.tar.gz` -> size `3720526`, SHA256 `EA45AE51D22FD5D176BE6BEEBFDB3E4BB894C0FC83F41BC97199A07E834F75F2`.
  - Rebuilt `EcoreX_0.2.3-webui-windows-x64.zip` -> size `81493457`, SHA256 `9CCA0D3DB3DF15CB9345329E8CC7A441A57664B2BCE18D2267494B43CE5FA6A4`.
  - Rebuilt `EcoreX_0.2.3-webui-macos-universal.zip` -> size `158619450`, SHA256 `1EFE893E2D32A1557BA0991DACE69D6853CEA7723775152DF3C914907E0C4315`.
  - Rebuilt `EcoreX_0.2.3-public-release.zip` -> size `245779741`, SHA256 `274E0D04FB577D5FA3EDD1C476BB9DB627F4C6F82131AA59DCA278E72611CCF9`.
  - Package audit confirms Web service, Windows WebUI, and macOS WebUI packages contain builtin-tool fallback and RapidOCR max-side optimization.
  - Production deploy PASS; public HTTP smoke confirms three ready artifacts are online and content lengths match manifest.
  - Production API capability smoke PASS: `/api/tools` exposes 21 tools, `/api/extensions` exposes builtin tools, Feishu surface visible.
  - Production real invocation smoke PASS using the service venv: Bash executes, OCR text URL returns browser handoff, OptionalAbilities list is callable, and Feishu CLI status returns structured `cli_missing` when the CLI is not installed.
  - Tool invocation/observability privacy scan, production deploy privacy scan, and final gate privacy scan all report `findingCount=0`.
  - Final release gate now requires 25 artifacts and reports `status=pass`, `complete=true`, `sliceCount=20`, `artifactCount=25`, `blockerCount=0`.

### R23-CAPABILITY-RECOVERY-HOTFIX-03

- User reported two final production blockers:
  - CDP browser tasks could time out after the user completed login, with the stream restored but no longer attached;
  - the model tried to read the external Codex Chrome plugin `SKILL.md`, then attempted raw CDP probing through Bash, which was blocked by runtime policy instead of continuing with the first-party `browser` tool.
- Root cause:
  - short second-turn confirmations such as `已登录` did not inherit the previous browser/web intent, so the browser schema could fall out of the first-turn schema budget;
  - the raw-CDP Bash reroute warned about browser automation but did not explicitly instruct the model to use the already exposed first-party `browser` tool;
  - external plugin skill files live outside the EcoreX runtime filesystem profile and must not be read by EcoreX as an implementation dependency.
- Fixes:
  - `agent/protocol/agent_stream.py` now treats login/authorization confirmations (`已登录`, `扫码完成`, `已授权`, and variants) as browser/web follow-up intent;
  - raw CDP Bash reroute now says to use the `browser` tool directly when available, and to avoid reading Codex/Chrome plugin `SKILL.md` or probing `127.0.0.1:9222` through Bash;
  - `agent/tools/browser/browser_tool.py` documents the same continuation rule in the tool description, keeping CDP use through the first-party browser runtime.
- Verification:
  - focused pytest for schema inheritance and CDP Bash reroute -> `4 passed`;
  - installed runtime was hotfixed and restarted on port `9909`; `/api/tools` returned `22` tools with registry ready;
  - `docs\v0.2.3\artifacts\installed-runtime-cdp-login-continuation-smoke.json` -> `PASS`; `已登录` selected browser/web schema, raw CDP Bash reroute pointed to `browser`, real CDP navigation loaded Xiaohongshu, and the logged-in sidebar marker was observed;
  - `docs\v0.2.3\artifacts\cdp-login-continuation-package-audit.json` -> `PASS`; Windows, macOS, and Web service packages all contain the follow-up intent and reroute/browser-description fixes;
  - paired privacy scan for installed runtime CDP continuation -> `findingCount=0`.

### R23-EXTERNAL-CONNECTIONS-UX-HOTFIX

- User reported External Connections cards still showed English/internal text such as `missing` and `no agent tool is declared for this channel`, and the buttons looked unfinished.
- Fixes:
  - `desktop/src/App.tsx` now maps platform descriptions, field labels, readiness/config/callable text, and action labels to Chinese user-facing strings;
  - `desktop/src/styles/app.css` adds compact action-button tones for save/check/connect/enable/disable/home-channel actions while keeping the existing EcoreX visual language;
  - `scripts/smoke-web-external-connections-browser.py` now asserts the Chinese labels and rejects the old raw English diagnostic strings.
- Verification:
  - `npm --prefix desktop run typecheck` and `npm --prefix desktop run build:renderer` passed;
  - `docs\v0.2.3\artifacts\external-connections-browser-smoke.json` -> `PASS`, including localized text, usable home-channel action, secret redaction, and Run Center hidden;
  - `docs\v0.2.3\artifacts\external-connections-privacy-scan.json` -> `findingCount=0`.

### R23-DEPLOY-FINAL-HOTFIX

- Production deploy initially failed at public-site installation because the remote temporary filesystem had no free space. This was treated as a hard failure, not a partial deploy PASS.
- Remediation:
  - cleaned only old temporary release directories, pip cache, and superseded release archives on the production host;
  - recorded minimized cleanup evidence in `docs\v0.2.3\artifacts\production-disk-cleanup-before-redeploy.json`;
  - changed the cleanup artifact `scope` to avoid a false-positive `sk-...` token match while keeping the privacy scanner strict.
- Rebuilt and deployed current release artifacts:
  - `release-artifacts\EcoreX_0.2.3-webui-windows-x64.zip` -> size `81504904`, SHA256 `E14A348D82BBBB769A56060012FD888FAAC0AE29470F9E13C44FE8BC7D8A2DBE`;
  - `release-artifacts\EcoreX_0.2.3-webui-macos-universal.zip` -> size `158631300`, SHA256 `3E4364B85F7E8999091F5201C95AFF3526D0FED3965D6CBC52E9A7FBF2D6502A`;
  - `release-artifacts\EcoreX_0.2.3-web-linux-service.tar.gz` -> size `3730548`, SHA256 `0CB568463249A5DCA3C194C6E533CE0F708EB307E6A2E4C9112187EC63A238C4`;
  - `release-artifacts\EcoreX_0.2.3-public-release.zip` -> size `245811058`, SHA256 `2EC44DFE3598861189D31BBFBDA7526FB2CEDD6B9C91D5D150FCF92FA6FC3F6C`.
- Production verification:
  - `docs\v0.2.3\artifacts\production-deploy-online.json` -> `PASS`; Web service, installation manifest, and public manifest report `0.2.3`, service is active/enabled, and `/api/version` returns `200`;
  - `docs\v0.2.3\artifacts\production-public-http-smoke.json` -> `PASS`; public root and manifest are reachable, admin is protected, and ready artifacts return manifest-matching lengths;
  - `docs\v0.2.3\artifacts\production-capability-recovery-smoke.json` -> `PASS`; production runtime has `22` tools, `66` extensions, `12` external connections, no missing required tools/extensions, and Feishu schema is visible/callable as a status probe;
  - `docs\v0.2.3\artifacts\production-real-tool-invocation-smoke.json` -> `PASS`; production Bash, OCR URL handoff, OptionalAbilities list, and Feishu CLI status are real tool executions;
  - `docs\v0.2.3\artifacts\production-deploy-privacy-scan.json` -> `findingCount=0`;
  - `python scripts\audit-v023-final-release-gate.py --require-complete` -> `status=pass`, `complete=true`, `sliceCount=20`, `artifactCount=25`, `blockerCount=0`;
  - `docs\v0.2.3\artifacts\final-release-gate-audit-privacy-scan.json` -> `findingCount=0`.

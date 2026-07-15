# v0.1.19 Development Log

## 2026-06-22

- Started implementation on branch `codex/ecorex-v0.1.19`.
- Confirmed the worktree was clean; the previously observed
  `desktop/scripts/stage-runtime-win.ps1` diff was not present.
- Created rollback baseline commit `c63b514 chore: baseline before v0.1.19`.
- Implemented bounded artifact verification filtering and portal-based artifact
  action menus, with actions disabled unless the artifact is verified ready.
- Added Codex-like recovery metadata and inline controls for stalled, failed,
  interrupted, replay-gap, and retryable-conflict assistant messages.
- Added `POST /api/requests/{request_id}/retry-prepare` to prepare safe retry
  drafts without automatically re-executing uncertain work.
- Implemented interrupt-and-send frontend admission state using
  `client_attempt_id`, `interrupts_request_id`, latest-wins guards, and local
  draft restoration for backend conflict/backpressure.
- Added right-click "add to current chat" support for durable local user
  attachments, assistant artifacts, markdown local links, and media steps, with
  normalized path dedupe.
- Hid Run Center from ordinary UI behind a development gate while preserving
  recovery and diagnostic surfaces internally.
- Added persisted sidebar collapse state for project and general session
  groups with auto-reveal for active/search/running/unread rows.
- Stabilized renderer visual smoke after v0.1.19 sidebar collapse changed row
  selectors and after 100K stream stress exposed Playwright screenshot/font wait
  limits. The 100K action now records metrics-only stress evidence instead of
  rewriting DOM or creating placeholder screenshots, so visual evidence remains
  real screenshots and stress evidence remains explicit metrics.
- Fixed post-done artifact tail recovery after review found a renderer smoke
  failure: tail artifacts are buffered and trigger a history refresh merge after
  arrival so a done-triggered history refresh cannot lose late artifacts.
- Hardened chat add-to-current-chat context menu: every add action re-verifies
  the resolved local path with `statLocalPath`, rejects `/uploads` preview-only
  items as composer attachments, and clamps context menus to the viewport.
- Hardened sidecar interruption recovery before `/message` admission by
  cleaning confirmed dead-owner session locks and marking orphan active message
  runs as `SIDECAR_INTERRUPTED` before same-session backpressure checks.
- Hardened the shared `SessionLock` implementation so a stale lock whose owner
  process is still alive on the current host is preserved instead of being
  unlinked by admission. This protects long-running WIN/MAC/Web tasks from
  duplicate admission caused only by wall-clock stale age.
- Hardened `/uploads` serving by resolving real paths and validating with
  `_is_within_directory` instead of prefix checks.
- Corrected retry-prepare semantics so history-fallback drafts are useful but
  not marked as `exactReplay` unless the original request metadata contains the
  visible user message.
- Updated both bundled and current workspace copies of
  `create-xiaohongshu-note` to generate final images with `gpt-image-2-pro`
  only. The cover helper no longer accepts `--fallback-model`, records
  `provider=openai`, `image_kind=final`, `draft=false`, and fails closed instead
  of producing local draft/placeholder imagery.
- Updated both bundled and current workspace copies of the general
  `image-generation` skill so default image creation uses `gpt-image-2-pro`
  only. OpenAI no longer auto-falls back to `gpt-image-2`; default routing uses
  OpenAI first, LinkAI only as a GPT Image compatible route when OpenAI is not
  configured, and does not drift to Gemini/Seedream/Qwen by default.
- Closed image-routing review blockers: `create-xiaohongshu-note` now trusts
  cached output only when the status provenance matches OpenAI
  `gpt-image-2-pro`, final image metadata, prompt hash, output path, and image
  SHA256; stale placeholder/Python outputs are deleted before dry-run or real
  generation. The general `image-generation` provider builder now prevents
  stale `SKILL_IMAGE_GENERATION_PROVIDER`/`provider` hints such as Gemini or
  DashScope from routing `gpt-image-2-pro` to non-GPT Image provider families.
- Bumped runtime and desktop package metadata to `0.1.19`, including the
  renderer/electron lockfile top-level version and v0.1.19 smoke output path.
- Expanded renderer visual smoke to cover the artifact overflow menu on a
  390x760 viewport and right-click file add-to-chat. Evidence records
  `.artifact-action-menu-portal` viewport metrics and composer attachment count.
- Synchronized the updated image skills into the current workspace overlay under
  `C:\Users\user\EcoreX\skills\...` after the final image-routing fixes.

## Image Generation Discussion

The user added a discussion requirement after the main v0.1.19 plan: current
image generation can still be handled by Python-created images instead of
`gpt-image-2-pro`.

Three independent read-only subagents reviewed the issue from separate angles:

- Model/config routing: traditional `ContextType.IMAGE_CREATE` can call
  `OpenAIImage.create_img`, whose default is `gpt-image-2-pro`, but agent-mode
  image requests can be captured by an image-generation skill or generic agent
  tooling instead of that route.
- Backend/tool execution: with `agent=true`, `IMAGE_CREATE` is not a hard
  execution constraint; the generic agent can use `bash`, Python/PIL,
  matplotlib, browser screenshots, and `send` to produce image artifacts.
- Frontend/artifact delivery: generated image outputs should be stored as
  durable workspace/project artifacts with verified local `path` plus optional
  preview URL; `/uploads` alone is preview-only and not enough for add-to-chat.

Consensus production fix:

- Add structured intent classification before agent execution:
  `ai_image_generation`, `image_edit`, `code_visualization`, `screenshot`,
  `document_render`, `vision_analysis`, and `text_chat`.
- Hard-route `ai_image_generation` to a typed image generation service/tool
  targeting OpenAI `gpt-image-2-pro`; do not expose generic `bash/browser`
  replacement paths for that intent.
- Keep Python allowed for explicit code visualization, charting, screenshots,
  and post-processing, but label those artifacts with provenance such as
  `generationKind=code_visualization` or `producer=python`.
- Fail closed with clear diagnostics when the configured OpenAI image provider
  or model is unavailable; any fallback to `gpt-image-2` must be explicit and
  telemetry-visible, not silent Python substitution.
- Add artifact provenance fields: `generationKind`, `producer`, `provider`,
  `model`, `sourceTool`, `toolCallId`, and `promptHash`.

Implemented v0.1.19 skill-level hardening from this discussion:

- `skills/create-xiaohongshu-note/scripts/generate_cover_image.py` now uses
  final-image-only `gpt-image-2-pro`; it rejects non-pro models and removes the
  fallback-model parameter.
- `skills/image-generation/scripts/generate.py` now defaults to
  `OpenAIProvider.DEFAULT_MODEL` (`gpt-image-2-pro`) and OpenAI no longer has an
  internal `gpt-image-2` model fallback.
- The current workspace overlays under `C:\Users\user\EcoreX\skills\...` were
  synchronized from the updated bundled skills so runtime use does not keep a
  stale fallback copy.

## Network Disconnect Root-Cause Follow-Up

On 2026-06-23 the user reported that a visible network interruption stayed in a
manual recovery state and did not automatically continue. Three read-only
subagents reviewed backend retry behavior, frontend recovery UI, and runtime
gateway/configuration:

- Backend finding: stream retry taxonomy correctly marks network interruptions
  as retryable, but when assistant output or tool-call arguments have already
  started, retry is deliberately suppressed with
  `retry_suppressed_reason=stream_output_started` to avoid duplicate output,
  duplicate tool execution, and duplicate file writes.
- Frontend finding: `EventSource.onerror` and reconnect exhaustion only tried
  stream reattach/history recovery. They did not consistently set the inline
  recovery state, so users could see a stalled message without a clear
  Codex-like `Recover` / `Retry draft` path.
- Environment finding: the local desktop runtime was using a cached enterprise
  model policy that selected a non-official OpenAI-compatible gateway. The
  host is intentionally redacted from release docs. Redacted local logs suggest
  mixed provider capability coverage, so the code fix avoids blaming one host
  and instead hardens the generic stream recovery path.

Implemented follow-up:

- `AgentStreamExecutor` now passes configured `model_max_retries` /
  `max_model_retries` into `LLMRequest`, so pre-output retry behavior is
  actually governed by config.
- Stream error chunks now record retry evidence on the agent error event:
  taxonomy, retryable, retry exhausted/suppressed, suppression reason, retry
  attempt, max retries, status code, terminal reason, and retry mode.
- `WebChannel` now preserves this evidence on SSE `type:error` events and
  records terminal reason such as
  `model_retry_suppressed_stream_output_started` in the durable run ledger.
- Renderer recovery UI now sets a stalled recovery card on every
  `EventSource.onerror`. Reconnect exhaustion produces a failed recovery state
  with `Recover`, `Retry draft`, and `Diagnostics`; if the backend still reports
  the original run active but the stream is unavailable, the UI exits pending
  and exposes `Recover`, `Stop`, and `Diagnostics` instead of looping forever.
- The implementation intentionally still does not blindly start a new run after
  output has begun; it follows the Codex-like safety rule that replay after an
  uncertain partial stream requires explicit user confirmation via retry draft.

Review fixes:

- Terminal SSE error payloads no longer advertise `retry_mode=auto_retry`;
  backend evidence and renderer recovery both normalize terminal retry mode to
  manual retry preparation or unavailable.
- Release docs were scrubbed of exact enterprise gateway host and production
  Admin DB backup path values. Future docs should keep provider/gateway hosts
  redacted unless the user explicitly asks for a private diagnostic note.
- Added a backend SSE behavior regression test for retry metadata and reran the
  renderer visual smoke to back the reconnect acceptance entries with more than
  source-marker checks.

## Release Continuation: Current-Branch Web Artifacts

On 2026-06-23, after the user opened the GitHub billing and upgrade pages, the
release path was rechecked from current state.

- GitHub macOS workflow run `27971223382` was dispatched against
  `codex/ecorex-v0.1.19` at
  `ee5b59449d270e2a9c64c0445eb4b739d7602fb0`. Both macOS jobs still failed
  before any runner step started; the run JSON showed empty `steps` arrays and
  failed-log retrieval returned `log not found`.
- Windows signing preflight still failed: SimplySign Desktop was running, but
  `SCardSvr` and `CertPropSvc` remained stopped and the SimplySign CSP key
  containers were not visible to the current process.
- A production packaging risk was found before rebuilding Web artifacts:
  `desktop/runtime/ecorex-runtime` had not fully inherited the latest
  `agent_stream.py` retry evidence changes. `npm run build` and
  `desktop/scripts/stage-runtime-win.ps1 -WinArch x64` were run to regenerate
  the renderer/electron build and restage the desktop runtime from the
  repository root.
- Current-branch Web artifacts were built into
  `release-artifacts/current-ee5b5944` and validated structurally. The Linux
  service package passed `check-ecorex-web-release.sh` with installed/HTTP
  checks disabled, and archive marker validation confirmed that the Linux
  service tarball plus all WebUI zip variants include the latest network
  recovery markers and the current renderer chunk.
- These Web artifacts were intentionally not uploaded to GitHub Release or the
  public site yet. Updating only Web assets would create a mixed release where
  GitHub tag `v0.1.19` still points to `b52999b0...` and signed Win/Mac desktop
  assets are not rebuilt from the same current branch head.

## Windows/Web Release Refresh

On 2026-06-23 the user narrowed the release scope to signed Windows desktop and
Web Win/Mac packages, with macOS desktop DMGs temporarily deferred.

- The requested signing path `C:\脚本签名工具` was inspected. Its
  `signtool.exe` successfully signed a temporary probe file with the expected
  Certum code-signing certificate, so the Windows package flow was allowed to
  bypass the stricter SimplySign CSP container preflight.
- `npm run package:win:all:signed` rebuilt and signed both Windows x64 and
  ia32 installers. Installed smoke passed for both architectures, including
  renderer nonblank checks, sidecar readiness, auth readiness, negative auth
  endpoint coverage, and cleanup.
- After the ia32 desktop package, `npm run stage:runtime:win:x64` was run again
  before WebUI packaging so the Windows WebUI zip uses an x64 runtime.
- Web Linux service, WebUI Windows x64, WebUI macOS universal, and combined
  WebUI Win/Mac packages were regenerated. Archive validation confirmed the
  current renderer chunk and network recovery markers are present.
- `update-ecorex-desktop-release-manifest.ps1` now supports refreshing Web
  artifact metadata, preventing manual manifest edits for WebUI/Web service
  size and SHA256 changes.
- The public release zip was rebuilt and deployed to the production download
  host. Server-side and public verifier checks passed with zero blockers.
- GitHub Release `v0.1.19` assets for Windows and Web were replaced with the
  refreshed files. macOS desktop DMG assets were intentionally left unchanged.

## WebUI Follow-Up: Folder Picker, macOS Installer, Feishu CLI

On 2026-06-23, user testing of the WebUI-first build found follow-up blockers:

- Windows native project-folder picker can open away from the browser and leave
  the WebUI with no visible pending state.
- macOS online installer can fail with `resume_args[0]: unbound variable` on
  Apple Silicon because macOS `/bin/bash` 3.2 treats empty array expansion under
  `set -u` as an error; this is architecture-independent and affects Intel too.
- Feishu capability enablement can show as installed while local CLI auth is
  still missing, and raw `lark-cli` shell calls are correctly blocked without a
  sufficiently visible structured `feishu_cli` path.

Initial fixes applied in source:

- `deploy/ecorex-site/install-webui.sh` now builds one non-empty curl argument
  array and conditionally appends resume/retry flags, avoiding Bash 3.2 empty
  array expansion failures.
- `channel/web/web_channel.py` now raises a topmost owner form for the Windows
  folder picker and emits Web bridge picker state events/fallback errors.
- `desktop/src/App.tsx` now shows immediate project-picker pending state,
  disables duplicate add-project clicks, avoids unnecessary double registration
  when `/api/project-folder/choose` already returned a complete project, and
  exposes a Feishu/Lark CLI ability row.
- `agent/protocol/agent_stream.py` now keeps `feishu_cli` in the core tool
  schema set so Feishu tasks do not depend on keyword budgeting to see the safe
  structured tool.
- `agent/tools/feishu_cli/feishu_cli.py` now honors a configurable writable
  install root, adds that root to PATH for command resolution, and reports a
  stable `authState` from short bounded status checks.
- `scripts/prepare-ecorex-webui-local-release.ps1` now generates Win/Mac WebUI
  configs with `tools.feishu_cli.install_root` under the writable state
  directory, logs the structured on-demand install path, starts macOS with an
  absolute `app.py` path, and improves stale macOS service discovery.

## WebUI Performance and Reconnect Structural Fixes

On 2026-06-23 the user reported that the WebUI on Windows and macOS still felt
very sluggish compared with WorkBuddy on the same hardware: typed text appeared
late, switching/deleting/folding sessions lagged, and streaming output felt
batched instead of live. The visible recovery bubble also suggested manual
recovery before the browser had a chance to reconnect to the same SSE stream.

Parallel read-only analysis split the issue into three root-cause groups:

- Renderer state topology: composer text, message deltas, session UI state, and
  sidebar lists were coupled in `App.tsx`, so high-frequency input/stream events
  could invalidate too much UI.
- Streaming/runtime transport: backend deltas were emitted one SSE frame at a
  time and assistant content used repeated string concatenation; frontend
  EventSource `onerror` treated transient reconnect attempts as immediate
  failures.
- Persistence/query load: history pagination scanned and sliced full visible
  histories; runtime snapshots refetched heavy capability/tool/skill/model data
  during ordinary lightweight refreshes.

Implemented structural changes:

- Composer draft input is now DOM/ref-first and committed to React state on a
  short debounce or explicit send/restore boundary, so ordinary typing no longer
  drives a full app render per key.
- Assistant stream assembly now stores content parts and joins on message end,
  avoiding O(n^2) string growth on long streaming replies.
- Web SSE output coalesces `delta` and `reasoning` frames with short timed/size
  flushes and explicit boundary flushes before tool events, permissions, errors,
  cancellation, and message end.
- `openMessageStream` now lets native EventSource perform safe automatic
  reconnect for a bounded transient window before surfacing manual recovery; it
  does not automatically duplicate execution when state is uncertain.
- Runtime capabilities are cached separately from lightweight runtime state, so
  ordinary refreshes keep `/api/version`, `/api/sessions`, and
  `/api/active-requests` hot without repeatedly pulling tools/skills/models.
- Conversation history pagination now loads recent visible user-turn windows
  from indexed sequence boundaries instead of always materializing the full
  session history and slicing in Python.
- User-facing release notes no longer mention Run Center while the diagnostic
  surface remains development-gated.

Local source WebUI smoke:

- Current source service was launched on `127.0.0.1:9926` using the repository
  `app.py`/`web_channel.py` and current renderer. The first performance smoke
  used `assets/index-D6uvzLTu.js`; the final post-review package uses
  `assets/index-BVtTilSA.js`.
- Headless Edge CDP smoke confirmed one visible composer textarea, no login
  page, no ordinary Run Center text, and release notes JSON without `Run Center`.
- Synthetic 3374-character input was visible in the textarea in `71.2ms`, with
  p95 per-input dispatch `0.1ms`, max `1.2ms`, and focus remaining on the
  textarea.
- Safe interaction smoke measured New chat `16.6ms`, Project collapse `32.6ms`,
  and General sessions collapse `32.3ms`. A prior attempt that clicked Add
  project folder was discarded because it correctly opened a native folder
  picker and blocked headless automation.
- UI/performance review found one P1 race: inactive background stream updates
  could fall back to a stale `sessionUiState` closure after a quiet tool/model
  gap. The fix introduced `committedSessionMessageSnapshots`, updated it on
  active/inactive message commits, and prevented stale `sessionUiState` effects
  from overwriting active or pending message baselines. Darwin re-reviewed the
  specific P1 and returned PASS.
- Final source WebUI CDP smoke against `assets/index-BVtTilSA.js` confirmed a
  visible composer, no login page, no ordinary Run Center text, and 3412
  characters reaching the textarea in `5.7ms` with focus still on `TEXTAREA`.

## Feishu Group Extraction Finalization Fix

On 2026-06-23 the user reported that a local WebUI task for Feishu group-chat
message extraction had found a result/conclusion, but the final answer was not
displayed as the stable last response. The visible answer also appeared to keep
thinking or looping after a partial conclusion.

Local log/history inspection of session `ecorex-1782217576934` showed the root
cause was not frontend truncation. The run discovered 9 active Feishu groups and
started `im +chat-messages-list` calls, but the agent loop guard collapsed all
those calls into the same chain key, `feishu_cli:run:im`. The first three group
message reads completed, then the remaining six distinct chat IDs were blocked
as repeated probing. The completed answer therefore contained only partial
results plus a blocker statement.

Implemented fixes:

- `AgentStreamExecutor` now keys Feishu IM message reads by subcommand, chat or
  user target, page token, time window, and sort order. Distinct group-message
  reads are allowed in one batch, while true repeats against the same
  target/page remain protected by the existing loop budget.
- Tool schema intent matching now uses ASCII word boundaries, so words such as
  `database` no longer accidentally select Feishu Base tooling through the
  `base` keyword. Explicit `feishu_cli` requests still select the safe
  structured tool, and MCP-present fallback avoids choosing `feishu_cli` for
  unrelated prompts.
- `AgentStreamExecutor` now enforces a finalization invariant: a non-cancelled,
  non-empty `final_response` is mirrored into the message list as an assistant
  text block if it is not already present.
- `AgentBridge` adds the same idempotent safety net before persistence, while
  skipping cancellation markers so cancelled runs do not receive duplicate
  synthetic assistant rows.
- `ConversationStore.get_latest_pair_seqs()` now prefers the latest assistant
  row with visible text rather than a tool-use-only assistant row, keeping retry
  and recovery metadata pointed at the user-visible final answer.

Validation:

- Full backend regression file passed with plugin autoload disabled:
  `233 passed, 3 warnings, 11 subtests passed`.
- A local core hand-test verified distinct Feishu chat batches, final text
  persistence, cancellation no-duplicate behavior, and assistant-text `bot_seq`.
- Independent read-only reviewers reached PASS consensus after fixing one
  explicit-tool selection blocker and one cancellation-history blocker.
- The 30-task production validation matrix passed with `30/30` tasks green and
  wrote durable evidence to `docs/v0.1.19/real-task-matrix.json`. The matrix
  checks the deployed WebUI artifacts, GitHub Release digests, Win/Mac package
  runtime markers, online installer resume markers, Feishu multi-group loop
  budget behavior, tool-selection boundaries, final-response persistence,
  cancellation no-duplicate behavior, and visible assistant-text history seqs.

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

# EcoreX v0.3.0 Architecture, Runtime, State Machine, Skill/MCP Audit

Date: 2026-07-07
Scope: WebUI-only production hardening audit after v0.3.0 implementation slices.

## Audit Surface

- WebUI task control: `desktop/src/App.tsx`, `desktop/src/services/ecorexApi.ts`, `channel/web/web_channel.py`.
- Runtime state and replay: `agent/protocol/run_ledger.py`, `agent/protocol/run_event_ledger.py`, `agent/protocol/runtime_projection.py`.
- Tool runtime: `agent/tools/tool_manager.py`, `agent/tools/browser/browser_service.py`, `agent/tools/imagegen/imagegen.py`, `agent/protocol/image_job_service.py`.
- Skill/MCP governance: `agent/skills/*`, `agent/extensions/registry.py`, `agent/tools/mcp/*`.
- External connectors: `channel/channel_catalog.py`, `channel/web/web_channel.py`, `desktop/src/App.tsx`.
- Release/update/admin: `scripts/release-ecorex-webui-orchestrator.ps1`, `scripts/prepare-ecorex-webui-local-release.ps1`, `deploy/ecorex-site/manifest.json`, `deploy/ecorex-admin-api/ecorex_admin_api.py`.

## Production Standard

- User intent should be interruptible and explainable; queueing is explicit, not the default.
- Runtime state must be replayable after refresh, crash, cancel, or reconnect.
- Tool output ordering must be deterministic and independent from provider timing.
- External connection setup must expose one clear next action, not raw config surfaces first.
- Release/update chain must stop on trust failure and never promote unsigned or unverified artifacts.
- Admin surfaces must show risk and state, not only mutate config.

## What Changed In v0.3.0

- Active turn control now defaults same-session new messages to `replace`; explicit menu offers `更新任务`, `排队稍后执行`, and `新开分支`.
- Backend `/message` accepts `interrupt_mode: replace/amend/queue/branch` and no longer treats queue as the default active-turn response.
- Runtime replay keeps request/event ledgers as the durable source for run state, image jobs, and external connection events.
- CDP/browser calls retry once after stale action results and avoid poisoning the next call after a disconnect result.
- Image generation artifacts carry `task_index` and `artifact_index`; frontend and runtime projection sort by those indexes.
- Imagegen routing now has three boundaries: intent discovery maps semantic generation/precise retouch/single-character image text edits to `imagegen`; schema selection exposes `imagegen` or diagnostic/enablement tools only; execution blocks bash/Python/PIL/OpenCV/ImageMagick/SVG/canvas as semantic edit substitutes.
- Online update state machine is explicit: `available -> downloading -> verified -> staged -> deferred -> installed -> activated -> rollback`.
- Admin release promotion blocks missing/invalid release-index, signatures, hashes, and smoke evidence.
- External connectors now have a Workbuddy-like quick panel for implemented connectors only. Planned/unimplemented products are not exposed as connectable UI entries.
- Configured workspace MCP servers are discovered through `ToolManager.ensure_mcp_configured_loaded()` across capability snapshots, skill/extension binding, agent initialization, streaming turns, and Tencent Docs attachment flows.
- Online update activation now includes external connector preservation checks. A version switch must keep previously connected/callable external tools discoverable, otherwise update state becomes `rollback`.

## Findings And Next Optimizations

### P0/P1 Before Public v0.3.0 Promotion

- Finish real user-path acceptance on an installed WebUI build:
  - conflict insert while a real task is running,
  - amend insert while a real task is running,
  - explicit queue cancel/promote,
  - CDP reconnect with a real browser task,
  - imagegen two-image provider output and batch ordering,
  - online update defer/activate/rollback.
- Sign Windows/macOS/Web service artifacts and promote `deploy/ecorex-site/release-index.json` only through the orchestrator.
- Produce Linux web-service artifact or remove it from the release-index contract before promotion.

### Runtime And State Machine

- Centralize active-turn decisions into a small backend state machine object instead of leaving branch/queue/replace checks across the `/message` handler.
- Add durable `interrupts_request_id`, `superseded_by_request_id`, and `branch_from_request_id` fields to the run ledger so UI copy does not infer replacement only from transient frontend state.
- Add a focused replay test that reconstructs a replaced run and verifies the old assistant message is shown as superseded after refresh.
- Add a queue watchdog that emits a user-visible stalled-queue event if a queued payload exists but no active worker starts within a bounded window.

### Skill And MCP Governance

- `ToolManager` now lazily loads MCP and tracks config signatures. Production next step is a UI-visible MCP lifecycle state: configured, starting, ready, failed, stale config, restarting.
- MCP tool names are public-normalized; add collision telemetry and a UI warning when two remote tools normalize to similar names.
- Skill-to-tool binding is already surfaced by `agent/skills/tool_binding_contract.py`; next step is an admin policy that can lock high-risk skill/tool bridges by environment and tenant.
- Add an installed-pack manifest for skill/capability runtime dependencies so WebUI can explain missing modules without a tool probe.

### External Connectors

- Keep the quick panel as the primary surface, but only for real implemented connectors: Tencent Docs MCP, Feishu, DingTalk, WeCom app/bot, and QQ channel where the backend projection exists.
- Do not expose Tencent Meeting, Tencent Survey, QQ Mail, Lexiang, ima, or finance connectors as buttons until EcoreX has real credential storage, backend tool calls, runtime discovery, and health probes for them.
- Store planned connector research in signed/admin-managed catalog data only after implementation begins; avoid frontend-only placeholder catalogs.
- Add one-tap connector flows for Tencent Docs and Feishu first; DingTalk/WeCom can stay credential-backed until official auth diagnostics exist.
- Each connector should expose: next action, auth state, tool readiness, last checked time, data scope, and disconnect/rollback result.

### Admin Productization

- Admin release page now blocks untrusted promotion. Next step is a release diff view comparing current/staged manifest, update-state policy, and rollback target.
- Add admin-managed connector policy: enabled connectors, allowed domains/tenants, auth method, and required data scopes.
- Add audit export for connector lifecycle events and capability-policy blocks.
- Add release/update acceptance that captures connector snapshots before and after an update for configured customer workspaces.

### Observability

- Standardize event names and states across run ledger, image jobs, external connections, scheduler, and update flow.
- Add a redacted support bundle schema per request/session that includes active turn control decisions, queue state, MCP status, and connector state.
- Add budget/latency histograms for imagegen, browser/CDP, MCP tool calls, and release/update health checks.
- Add provider-level real-user evidence for `imagegen` precise retouch: one annotated image with a single-character edit, one request returning two images, and one multi-task batch. Evidence must include tool route, artifact ordering, and no semantic bash/Python edit calls.

## Current Release Gate Status

- Source/type checks: pass for targeted v0.3.0 hardening tests.
- Renderer build: pass.
- WebUI Windows/macOS local package build: pass after replacing memory-heavy `Compress-Archive` path.
- Installed/package runtime smoke: pass for Windows extracted package `/api/version` and `/app/`.
- Full release-index promotion: not complete because signatures, full cross-platform smoke, and web service artifact trust are still pending.

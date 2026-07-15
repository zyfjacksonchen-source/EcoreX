# EcoreX Web Runtime Hardening Goal

## Scope

This goal is Web-only. Implementation may touch the Web service package, Web console, Agent common layer, and Runtime common layer. It must not require Electron, desktop renderer, or desktop sidecar changes.

## Objective

Make EcoreX Web core capabilities available out of the box, unify capability status and repair flows, make safety guardrails usable by default, and reduce Web/runtime technical debt instead of adding parallel special cases.

Target final release marker: `v0.2.6`.

## Global Constraints

- Do not add a second Web-only dependency state store.
- Do not rely on prompt-driven package repair for deterministic runtime dependencies.
- Do not expose host system PATH as the default runtime fix.
- Every completed slice requires multi-agent read-only review from architecture, security, runtime, Web UX/observability, and test/release perspectives.
- A slice passes only when all reviews are `PASS` or non-blocking `PASS_WITH_NOTES`, and the consensus record explains any notes.

## Evidence Layout

- `slices/Sxx-*.md`: slice design, changes, and acceptance criteria.
- `reviews/Sxx-consensus.md`: multi-agent review result and final decision.
- `artifacts/Sxx-*.json`: runtime snapshots, dependency reports, permission matrices, and test summaries.

## Current Status

- `S0` passed with notes: Web core runtime baseline and release-gate contract.
- `S1` passed with notes: high-risk config loading debt removal.
- `S2` passed with notes: Web core runtime becomes ready from the Web service package without relying on user system PATH.
- `S3` passed with notes: runtime packs and installer semantics now live in a shared public runtime layer with path confinement, URL redaction, host-runtime isolation, and configure-only state.
- `S3a` passed with notes: session auto-title now uses stored session summary/context rather than the latest message payload.
- `S4` passed with notes: CapabilityService is now the shared runtime/tools/skills/extensions/capabilities fact source; status probes may refresh unified capability-state diagnostics but do not install or repair.
- `S4b` passed with notes: Web multi-model chat selection uses admin-managed `gpt-5.5`, one real-connectivity-verified model per provider, provider icons, dynamic context policy, secret-clean release packaging, and image generation pinned to `gpt-image-2-pro`.
- `S5` passed with notes: capability-level authorization now governs Web APIs, AgentStream, scheduler, image jobs, Feishu external-connection auth, filesystem-backed Web helpers, and shared image job parallelism policy with fail-closed malformed broker handling and structured Web denial payloads.
- `S6` passed with notes: `/api/capabilities.visualWorkflow` now exposes deterministic Web-only image input, Fast OCR repair, vision fallback, and `gpt-image-2-pro` imagegen credential action plans without running installer probes or leaking provider keys on Web status GETs.
- `S7` passed with notes: Web console submit paths now converge through one pipeline, SSE/history/focus recovery reads runtime projection plus active requests, inline action rows are synchronized and sanitized, stale permission rows are removed at terminal state, and failed-submit recovery no longer duplicates stale sending phases.
- `S8` passed with notes: Web route table, auth, SSE, sessions, projection, files, image jobs, capabilities, diagnostics handlers, and inline action JS have been split into ownership modules; one raw exception log was converted to a redacted summary during review.
- `S9` passed with notes: Web release gate now generates and validates runtime baseline, capability state, permission matrix, review consensus, and release-gate artifacts; capability manifests now require deterministic repair/discover/configure actions and block private path/state fields. Final Web/runtime marker is `v0.2.6`.
- `S10` passed with notes: production Web full-access smoke on the public domain verified discovery, permission mode, Python/Node/npm/npx, OCR, Vision, Browser fallback, and real `gpt-image-2-pro` generate-then-edit image chain without Python image fallback.
- `S13` passed with notes: Tongxin Assistant CLI is bundled as a read-only Web runtime package, exposes `DATABASE`/`Database`/`database`, prefers bundled/local paths, validates real realtime consumption through `direct_account_id`, and the refreshed v0.2.6 packages/download page are deployed with postdeploy server evidence.
- `S15` passed with notes: macOS WebUI install zip no longer contains non-ASCII runtime paths, mac installer browser launch and old-service cleanup are hardened, release notes revision is `2026-07-02-mac-webui-r4`, regenerated v0.2.6 packages are deployed, and multi-agent review reached non-blocking consensus.

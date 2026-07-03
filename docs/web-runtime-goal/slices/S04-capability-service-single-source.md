# S4 CapabilityService Single Source

## Intent

Stop letting Web tools, skills, extensions, and capability packs report from separate ad hoc sources. S4 introduces a read-only common projection that Web handlers and `agent_capability diagnose` can share before S5 permission broker work and S6 image/OCR workflow closure.

Target release marker: `v0.2.6`.

## Implemented Changes

- Added `agent/runtime_capabilities.py` with:
  - `RuntimeCapabilityRegistry`
  - `CapabilityService`
  - `SERVICE_VERSION = web-runtime-goal-s4-v1`
- `/api/tools`, `/api/skills`, `/api/extensions`, and `/api/capabilities` now read through the runtime capability service.
- `agent_capability diagnose` now returns the same service projection instead of independently collecting abilities, skills, and MCP status.
- `/api/capabilities` keeps legacy `abilities`/`packs` compatibility while adding typed action-plan fields:
  - `state`
  - `missingItems`
  - `nextAction`
  - `actionLabel`
  - `retryable`
  - `diagnosticSummary`
  - `logRef`
- Ability entries also include `actionPlan` with the same typed fields for UI consumers that prefer a nested contract.
- `logRef` and `targetRef` are redacted to filename/parent-name metadata instead of exposing absolute paths.
- CapabilityService performs read-only `install-capability.py --action status` probes for unresolved capability packs so S3 manifest `moduleChecks` are reflected in Web state without triggering install/repair.
- `agent_capability list_packs` and `agent_capability diagnose` both use the same service projection.
- Feishu/Lark uninstalled state now projects as `discovery_only` with `nextAction=discover`.
- Configure-only capabilities such as `tongxin-cli` project as `needs_configuration` unless already configured.
- Extension entries for runtime abilities are enriched with the same action plan as `/api/capabilities`.
- Image generation remains represented as skill/tool binding rather than a runtime pack; S4 verifies `image-generation` skill, `imagegen` tool, and extension binding stay consistent.

## Acceptance

- Web routes are thin projections over `RuntimeCapabilityRegistry` / `CapabilityService`.
- `agent_capability diagnose` and `/api/capabilities` use the same backend service.
- Feishu/Lark, Tongxin, office/PDF, and fast OCR return deterministic typed action plans.
- Image-generation status is consistent across tools, skills, and extensions.
- No new Web-only state store is introduced.
- No install/repair action is performed by the S4 read-only service; status probes may refresh the unified capability-state files with deterministic diagnostics.

## Evidence

- `docs/web-runtime-goal/artifacts/S04-capability-service-tests.json`

## Remaining Notes

- S5 should route capability actions through the shared permission broker before install/configure/repair calls.
- S6 should add image upload/OCR/vision/imagegen workflow-specific readiness projections on top of this service.
- S7/S8 can thin Web console and handlers further now that capability facts have a shared backend.

# S6: Visual Workflow Closure

## Goal

Close the Web-only image workflow status gap for uploaded images, OCR, vision fallback, and image generation. The user should see deterministic action plans instead of a vague "environment dependency missing" message.

## Changes

- Added `visualWorkflow` to `CapabilityService.capabilities_payload()` and therefore `/api/capabilities`.
- Added an image input contract:
  - `imageInput.supported=true`
  - `imageInput.autoDetect=true`
  - accepted MIME prefixes include `image/`
- Added OCR action-plan projection:
  - ready local OCR reports `state=ready`.
  - missing Fast OCR dependencies report `nextAction=repair_fast_ocr`.
  - missing module names come from the `fast-ocr` capability pack.
- Added vision action-plan projection:
  - configured provider credentials report `state=ready`.
  - missing provider credentials report `state=needs_provider_credentials`.
  - missing credentials report `nextAction=configure_model_provider`.
- Added image generation action-plan projection:
  - image generation remains pinned to `gpt-image-2-pro`.
  - missing provider credentials report `state=needs_provider_credentials`, not `missing_tool`.
  - visible `imagegen` tool or `image-generation` skill marks the route as visible.
- Added `overall.visionFallbackAvailable` for OCR-missing but vision-ready scenarios.

## Boundaries

- This slice does not install OCR dependencies.
- This slice does not call real vision or image generation providers during status rendering.
- Web status endpoints for capabilities/extensions do not run installer status probes; explicit diagnose/repair flows own probe/repair side effects.
- This slice does not change desktop/Electron.
- This slice does not change the image generation model picker from S4b; imagegen remains `gpt-image-2-pro`.
- UI rendering of inline action rows can consume this contract in S7.

## Acceptance

- Uploaded image workflow can be identified through `/api/capabilities.visualWorkflow.imageInput`.
- OCR missing dependencies show `repair_fast_ocr`.
- OCR-ready scenarios show OCR as ready.
- OCR-missing with configured vision credentials exposes `visionFallbackAvailable=true`.
- Vision/imagegen missing provider credentials show `configure_model_provider`.
- `gpt-image-2-pro` missing credentials are not misreported as missing imagegen tool route.
- `/api/capabilities` and `/api/extensions` do not run `install-capability.py --action status` as a status-rendering side effect.
- Tests and syntax checks pass with S6 artifacts recorded.

## Evidence

- `docs/web-runtime-goal/artifacts/S06-visual-workflow-tests.json`
- `docs/web-runtime-goal/reviews/S06-consensus.md`

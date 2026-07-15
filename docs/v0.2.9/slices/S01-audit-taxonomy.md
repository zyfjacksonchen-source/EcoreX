# S01 Audit Taxonomy and Admin Projection

## Status

Completed.

## Intent

Expose clearer user behavior actions in the admin usage panel and remove low-signal visible metrics.

## Decisions

- Count image processing when `imagegen` is called.
- Do not expose local file processing as a top-level action.
- Keep the data projection compatible with existing sync events.

## Implementation

- `GET /runtime-audit` now exposes `actionTypeCounts`, `actionTypeLabels`, and `userActions`.
- Image processing is detected from `image_job.*` events and `tool.*` events whose synced detail references `imagegen`.
- User actions are projected as redacted business actions with request/session/user/device hashes, not raw ids.
- Low-level event/source/status counts remain available as technical detail for debugging, but are no longer the main UI surface.

## Verification

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q`
- `npm run typecheck` from `desktop/`

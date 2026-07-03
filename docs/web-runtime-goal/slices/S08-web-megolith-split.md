# S8: Web Megolith Split

## Status

Passed with notes on 2026-07-01.

## Goal

Reduce Web technical debt by splitting `channel/web/web_channel.py` and `channel/web/static/js/console.js` into clear ownership modules without introducing a new frontend framework or a second runtime state source.

## Implemented Split

- `channel/web/routes.py`: declarative Web route table consumed by `WebChannel.startup()`.
- `channel/web/auth.py`: `/auth/check`, `/auth/login`, `/auth/logout` handlers.
- `channel/web/sse.py`: `/stream` SSE handler.
- `channel/web/sessions.py`: sessions, history, message delete, clear-context, auto-title, and UI state handlers.
- `channel/web/projection.py`: active requests, retry prepare, and runtime projection handlers.
- `channel/web/files.py`: uploads, file preview, file stat, and JSON file read handlers.
- `channel/web/image_jobs.py`: image job status/start/action handlers.
- `channel/web/capabilities.py`: tools, skills, capabilities, and extensions handlers.
- `channel/web/diagnostics.py`: logs and diagnostics handlers.
- `channel/web/handler_support.py`: transitional auth/workspace/error helper bridge for extracted handlers.
- `channel/web/static/js/inline-actions.js`: ordinary JS module for inline action normalization, rendering, and synchronization.

## Boundary Notes

- Route handler class names remain exported from `channel/web/web_channel.py` through imports, so the existing `WEB_ROUTES` string-handler contract remains compatible.
- Runtime/security-heavy helpers remain in `web_channel.py` as a transitional service bridge where moving them would expand blast radius.
- Extracted handlers call legacy helpers deliberately; no new state store, permission source, installer path, or runtime projection source was added.
- `console.js` keeps wrapper functions for existing call sites; inline action implementation now lives in `inline-actions.js`, loaded before `console.js`.

## Evidence

- `docs/web-runtime-goal/artifacts/S08-route-table-anchor.json`
- `tests/test_web_runtime_goal.py::test_s8_web_routes_are_declarative_and_external_to_startup`
- `scripts/smoke-s7-inline-actions.js`

## Verification

- `python -m py_compile channel/web/auth.py channel/web/sse.py channel/web/sessions.py channel/web/files.py channel/web/image_jobs.py channel/web/projection.py channel/web/capabilities.py channel/web/diagnostics.py channel/web/web_channel.py tests/test_web_runtime_goal.py`
- `node --check channel/web/static/js/inline-actions.js`
- `node --check channel/web/static/js/console.js`
- `node scripts/smoke-s7-inline-actions.js`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "runtime_projection_api_returns_request_and_session_projection or web_stream_terminal_and_loss_converge_from_runtime_projection or web_history_load_refreshes_session_runtime_projection or image_jobs_api_starts_collects_and_projects_backend_job or web_file_serve_obeys_custom_filesystem_profile or capabilities_api_flattens_policy_capability_packs_for_frontend_contract"`

## Acceptance Criteria

- Route table is declarative and no longer assembled inside `WebChannel.startup()`.
- `web_channel.py` no longer defines extracted handler classes.
- Extracted handler modules preserve Web auth, permission broker, runtime projection, image job, file access, capability, diagnostics, and session behavior.
- `console.js` delegates inline action behavior to an ordinary JS module without introducing a framework.
- Full Web runtime goal tests pass before review.

## Review Gate

S8 passed multi-agent review across architecture, security, runtime dependency, Web UX/observability, and test/release perspectives. See `docs/web-runtime-goal/reviews/S08-consensus.md`.

# S7 Consensus: Web Console State Machine

## Final Verdict

PASS_WITH_NOTES

S7 is accepted. Initial review found blocking issues in stale permission action lifecycle, action-plan-only recovery rendering, submit-error permission routing, and executable coverage. Those findings were fixed and re-reviewed to `PASS` or non-blocking `PASS_WITH_NOTES`.

## Review Roles

- Architecture consistency: `PASS`
  - `submitMessage(opts)` is the single `/message` frontend submit path.
  - Web recovery uses runtime projection and active request snapshots without adding a Web-only state source or installer executor.
  - Action-plan-only projections are renderable and carried into newly created bot bubbles.
- Security and privacy: `PASS`
  - Permission decisions still go through `/api/tool-permissions` and the S5 broker.
  - Terminal projections no longer resurrect stale Allow/Deny rows.
  - Permission fallback actions use `view_capability_policy`; the old `open_permissions` fallback is removed.
  - Sensitive action text tests cover `api key`, `Authorization`, `Bearer`, and `sk-` variants.
- Runtime dependency correctness: `PASS_WITH_NOTES`
  - Inline repair/configure/policy rows do not invoke a Web-only installer.
  - `/api/active-requests` is a recovery snapshot with existing stale-run cleanup behavior, not a dependency installer side effect.
- Web UX and observability: `PASS`
  - SSE terminal refresh uses `syncInlineActionRows`, so stale permission rows are removed.
  - Submit failures clear the stale sending phase before rendering recovery rows.
  - Permission buttons remain disabled after a successful allow/deny decision until terminal projection removes the row.
- Tests and release: `PASS_WITH_NOTES`
  - S7 now has executable Node smoke coverage for actual `console.js` inline-action helper behavior.
  - Note: a future release gate should add a fuller browser/JSDOM lifecycle smoke for history/stream/focus/visibility active-request recovery.

## Blocking Findings Fixed

- `applyRuntimeProjectionSnapshot()` initially used append-only inline rendering; it now uses `syncInlineActionRows(...)`.
- Terminal backend projections initially retained pending permission action plans; they now drop `confirm_permission` plans once state is no longer `waiting_permission`.
- Action-plan-only projections could previously be skipped or render blank; they now count as renderable and flow through `runtimeProjectionBotMessageData()`.
- Submit permission failures previously normalized to a missing `open_permissions` action; they now use `view_capability_policy`.
- Failed submits could leave the old `agent-current-phase`; `renderSubmitFailureOnce()` now removes it.

## Evidence

- `docs/web-runtime-goal/artifacts/S07-web-console-state-machine-tests.json`
- `docs/web-runtime-goal/slices/S07-web-console-state-machine.md`

## Verification

- `node --check channel/web/static/js/console.js; node --check scripts/smoke-s7-inline-actions.js`
  - passed
- `node scripts/smoke-s7-inline-actions.js`
  - passed with `projectionOnlyRenderable=true`, `submitPermissionAction=view_capability_policy`, `permissionRowRemovedAfterTerminalSync=true`
- `python -m py_compile agent/protocol/runtime_projection.py tests/test_web_runtime_goal.py scripts/smoke-chat-model-connectivity.py`
  - passed
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q -k s7`
  - `4 passed, 42 deselected`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q`
  - `46 passed, 3 warnings`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "runtime_projection_api_returns_request_and_session_projection or web_stream_terminal_and_loss_converge_from_runtime_projection or web_history_load_refreshes_session_runtime_projection or web_runtime_real_network_browser_smoke_harness_contract or frontend_has_typed_runtime_projection_fetch_contract"`
  - `5 passed, 401 deselected, 2 warnings`

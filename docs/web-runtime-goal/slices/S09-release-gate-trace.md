# S9: Release Gate and Long-Term Trace

## Status

Passed with notes on 2026-07-01.

## Goal

Prevent future Web skills, apps, connectors, and runtime abilities from reintroducing per-capability environment patches, private state stores, prompt-driven repairs, or invisible permission bypasses.

## Implemented Gate

- Added `scripts/generate-web-runtime-release-gate.py` as the Web-only release snapshot generator.
- The generator writes:
  - `runtime-baseline.json`
  - `capability-state.json`
  - `permission-matrix.json`
  - `review-consensus.md`
  - `web-release-gate.json`
- The generator audits `runtime-packs/capabilities.json` for:
  - exact public `repairAction` on repairable packs;
  - exact deterministic `discoverAction` on discovery-only packs;
  - exact deterministic `configureAction` on configure-only packs;
  - forbidden private runtime/path fields such as `stateDir`, `state_dir`, `targetDir`, `target_root`, `logPath`, `configPath`, `installRoot`, `commandPath`, and `pathOverride`.
- The generator writes failure artifacts even when manifest parsing or permission-matrix generation fails.
- `scripts/install-ecorex-web.sh` now runs the release gate after the strict Web core baseline and before service startup.
- `scripts/install-ecorex-web.sh` only switches `$INSTALL_ROOT/current` after Python deps, Node, baseline, and release gate all pass.
- Online Web release installs now require `EXPECTED_SHA256`; local tarball installs still use `TARBALL_PATH`.
- `scripts/check-ecorex-web-release.sh` now requires and validates the S9 release gate artifacts.
- `scripts/prepare-ecorex-web-release.ps1` now includes the release gate generator in the Web runtime package.
- `runtime-packs/capabilities.json` and `desktop/runtime-packs/capabilities.json` remain byte-for-byte aligned as the shared manifest source.
- `common/ecorex_tool_permissions.py` now redacts Bearer credentials and JSON token/password/authorization fields before permission summaries or audit entries persist them.

## Permission Matrix

The release gate evaluates the real `ToolPermissionBroker` in a temporary user-data directory and a temporary probe workspace so the snapshot proves current guardrail behavior without mutating the user's admin settings or workspace files.

Covered modes:

- `read-only`
- `smart-ask`
- `custom`
- `full-access`

Covered boundaries:

- low-risk status/list/diagnose paths;
- workspace and artifact reads;
- auditable workspace writes;
- system shell execution;
- Feishu CLI read/write/admin classifications;
- background vs user-initiated image job creation.

## Acceptance Criteria

- Web release checks fail when core baseline, capability manifest, permission matrix, or capability state snapshots fail.
- Web install generates all required release-gate artifacts in `$STATE_DIR`.
- New repairable capability packs must declare public installer repair actions.
- New discovery/configuration packs must declare deterministic discover/configure actions.
- Manifest state/path fields cannot create private state stores or PATH overrides.
- Permission matrix is generated from the shared broker rather than hand-written.
- Failed release-gate runs still write Web release artifacts that explain the failing stage.
- Permission broker summaries redact Bearer and JSON-shaped credentials.

## Verification

- `python -m py_compile scripts/generate-web-runtime-release-gate.py common/ecorex_tool_permissions.py tests/test_web_runtime_goal.py`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q -k "s9_"`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q -k "web_core_baseline or s9_"`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "runtime_projection_api_returns_request_and_session_projection or web_stream_terminal_and_loss_converge_from_runtime_projection or web_history_load_refreshes_session_runtime_projection or image_jobs_api_starts_collects_and_projects_backend_job or web_file_serve_obeys_custom_filesystem_profile or capabilities_api_flattens_policy_capability_packs_for_frontend_contract"`
- `bash -n scripts/check-ecorex-web-release.sh`
- `bash -n scripts/install-ecorex-web.sh`
- PowerShell parse check for `scripts/prepare-ecorex-web-release.ps1`
- Release gate smoke with a ready baseline input and real manifest/permission matrix generation; confirmed no `release-gate-note.md` is written to the provided workspace.

## Review Gate

S9 passed multi-agent read-only review across architecture, security, runtime dependencies, Web UX/observability, and test/release perspectives. See `docs/web-runtime-goal/reviews/S09-consensus.md`.

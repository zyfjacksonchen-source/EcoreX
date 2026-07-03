# S9 Consensus Review

## Final Verdict

PASS_WITH_NOTES. S9 is accepted.

## Scope Reviewed

- Web release gate generator:
  - `scripts/generate-web-runtime-release-gate.py`
- Web install, package, and release check wiring:
  - `scripts/install-ecorex-web.sh`
  - `scripts/check-ecorex-web-release.sh`
  - `scripts/prepare-ecorex-web-release.ps1`
- Shared capability manifest:
  - `runtime-packs/capabilities.json`
  - `desktop/runtime-packs/capabilities.json`
- Permission redaction and release/version surfaces:
  - `common/ecorex_tool_permissions.py`
  - `common/ecorex_release_notes.py`
  - `cli/VERSION`
  - `pyproject.toml`
  - `deploy/ecorex-site/*`
- Tests and trace:
  - `tests/test_web_runtime_goal.py`
  - `tests/test_ecorex_web_parallel_backend.py`
  - `docs/web-runtime-goal/slices/S09-release-gate-trace.md`
  - `docs/web-runtime-goal/artifacts/S09-release-gate-tests.json`

## Role Results

| Role | Agent | Verdict | Blocking Items |
| --- | --- | --- | --- |
| Architecture consistency | Volta | PASS_WITH_NOTES | None |
| Security and permissions | Carson | PASS_WITH_NOTES | None |
| Runtime dependencies | Raman | PASS_WITH_NOTES | None |
| Web UX / observability | Zeno | PASS | None |
| Test / release | Kierkegaard | PASS_WITH_NOTES | None |

## Iterations Resolved

- Architecture initially failed because the permission matrix wrote `release-gate-note.md` into the provided workspace. It now uses a temporary probe workspace and records `providedWorkspaceMutated=false`.
- UX/observability initially failed because crash paths could leave only a traceback or stale pass artifacts. Failure paths now overwrite current release-gate artifacts with explicit failed snapshots and include errors in `review-consensus.md`.
- Security initially failed on early `current` switching, optional online tarball hash, incomplete credential redaction, and narrow private manifest field blocking. The installer now requires `EXPECTED_SHA256` for online installs, switches `current` only after gates pass, redacts Bearer/JSON credentials, and blocks normalized camel/snake private path fields.
- Test/release noted stale artifact naming and counts. The manifest audit artifact now points to `web-release-gate.json#manifestAudit`, and S9 evidence was refreshed after the added tests.

## Consensus

All reviewers agree S9 can pass. Notes are non-blocking:

- Manifest private-field blocking is still an explicit normalized list, not an unrestricted heuristic over every path-like key. This avoids false positives for current packs; future gates may add a stricter allowlist if new capability schemas grow.
- Baseline dependency rows still use legacy action labels such as `repair_core_node` and `repair_fast_ocr`; capability pack repair actions are already unified through `install-capability --action repair --pack-id <id>`.
- `check-ecorex-web-release.sh` validates matrix schema/status/blockers, while the no-workspace-mutation assertion lives in generator tests and the generated matrix field.
- The generated `review-consensus.md` is an automated per-release artifact; this file remains the human multi-agent slice consensus record.

## Pass Evidence

- `python -m py_compile scripts/generate-web-runtime-release-gate.py common/ecorex_tool_permissions.py tests/test_web_runtime_goal.py`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q -k "s9_"` -> `6 passed`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q -k "web_core_baseline or s9_"` -> `10 passed`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q` -> `53 passed`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "runtime_projection_api_returns_request_and_session_projection or web_stream_terminal_and_loss_converge_from_runtime_projection or web_history_load_refreshes_session_runtime_projection or image_jobs_api_starts_collects_and_projects_backend_job or web_file_serve_obeys_custom_filesystem_profile or capabilities_api_flattens_policy_capability_packs_for_frontend_contract"` -> `6 passed`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "version_handler_returns_user_facing_release_notes"` -> `1 passed`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "v020_webui_install_pages or v021_web_deploy_paths"` -> `2 passed`
- `bash -n scripts/check-ecorex-web-release.sh; bash -n scripts/install-ecorex-web.sh`
- PowerShell parse check for `scripts/prepare-ecorex-web-release.ps1`
- Release gate smoke with ready baseline input -> `releaseReady=true`, `blocking=0`, and no mutation of the provided workspace.

## Final Decision

S9 passes with notes. The long Web runtime hardening goal is ready to be marked complete for the Web service package/runtime scope.

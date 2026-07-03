# S5 Consensus: Capability Permissions And Usable Guardrails

## Final Verdict

PASS_WITH_NOTES

S5 is accepted. All blocking review findings were fixed and re-reviewed to `PASS` or non-blocking `PASS_WITH_NOTES`.

## Review Roles

- Architecture consistency: `PASS`
  - Final review confirmed `authorize_capability()` remains the single public permission boundary and image job parallelism policy now lives in `agent.protocol.image_job_service`, not the Web handler.
- Security and permissions: `PASS`
  - Final review confirmed Feishu structured install/config/auth actions, Web external-connection Feishu auth, bash/system shell, file/artifact paths, malformed broker decisions, and audit logging fail closed where required.
- Runtime dependency and execution chain: `PASS_WITH_NOTES`
  - Final review confirmed AgentStream and scheduler only use legacy broker fallback when `authorize_capability()` is absent. Malformed decisions, empty dicts, non-dicts, and MagicMock-style truthiness deny.
  - Note: local pytest requires `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` because an unrelated global pytest plugin imports an incompatible `pydantic-core`.
- Web UX and observability: `PASS`
  - Final review confirmed scheduler/image-job denials use structured `permission_denied` payloads with capability/action/mode, low-risk status paths still work, and deny-path tests prove broker refusal stops service reads.
- Tests and release: `PASS_WITH_NOTES`
  - Final review confirmed artifact JSON is valid, evidence commands are reproducible, all-mode low-risk matrix is covered, Web status deny paths are covered, hybrid malformed broker fallback is covered, and image job policy moved to the public layer.
  - Note about missing consensus file was resolved by this document.

## Blocking Findings Fixed

- `image_jobs` was not treated as dangerous in fallback authorization paths.
- Web capability helper could fall back to legacy/non-dict decisions.
- AgentStream and scheduler could fall through to permissive legacy methods after malformed `authorize_capability()` output.
- Model-supplied bash arguments could spoof lower-risk workspace actions.
- Read-only mode could be bypassed by remembered shell grants.
- Feishu `download`/`export` read-like commands could write local files without filesystem-profile authorization.
- Web file/artifact/project/open-path helpers treated malformed `authorize_file_access()` results as truthy allow.
- Low-risk permission matrix did not cover `smart-ask`, `read-only`, and `full-access` uniformly.
- Web scheduler/image-job status tests did not prove broker-deny paths.
- Image job parallelism policy lived in the Web handler instead of shared runtime.
- Feishu structured `install`, `config_init`, `auth_login`, `agent_auth`, and `authorize_agent` actions were default-allowed.
- Web external-connection Feishu `agent_auth` could call `FeishuCli.execute()` directly before broker authorization.

## Evidence

- `docs/web-runtime-goal/artifacts/S05-permission-guardrails-tests.json`
- `docs/web-runtime-goal/slices/S05-capability-permissions-guardrails.md`

## Verification

- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_web_runtime_goal.py -q`
  - `36 passed, 3 warnings`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -q -k "image_jobs_api_projects_auditable_parallelism_policy or image_jobs_vision_ocr_provider_requires_tool_permission or smart_ask_requires_permission_for_env_config_and_send or non_web_channel_dangerous_tools_still_fail_closed or read_only_blocks_scheduler_mutations or scheduler_background_execution_requires_noninteractive_permission or scheduler_tool_call_checks_target_tool_permission or tool_permission_handler_round_trips_mode_and_audit or default_filesystem_profile_limits_unprofiled_file_access_to_workspace or custom_filesystem_profile_limits_file_tools_to_workspace or web_file_serve_obeys_custom_filesystem_profile"`
  - `11 passed, 395 deselected, 2 warnings`
- `python -m py_compile common/ecorex_tool_permissions.py agent/protocol/agent_stream.py agent/tools/scheduler/integration.py channel/web/web_channel.py agent/protocol/image_job_service.py agent/protocol/__init__.py`
  - passed
- `python -m json.tool docs/web-runtime-goal/artifacts/S05-permission-guardrails-tests.json > $null`
  - passed

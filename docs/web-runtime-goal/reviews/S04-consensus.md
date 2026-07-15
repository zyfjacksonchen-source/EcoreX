# S4 Consensus Review

## Slice

`S04-capability-service-single-source`

## Final Decision

`PASS_WITH_NOTES`

S4 passes after resolving the initial security and runtime/installer blockers. All roles are now `PASS` or non-blocking `PASS_WITH_NOTES`.

## Review Results

| Role | Result | Notes |
| --- | --- | --- |
| Architecture consistency | `PASS_WITH_NOTES` | Initial note that `agent_capability list_packs` still bypassed the service was resolved by routing it through `CapabilityService`. |
| Security/permissions | `PASS_WITH_NOTES` | Initial raw `logPath` / `targetDir` leak was fixed. Remaining note: `CapabilityService` status projection may refresh unified capability-state files, but does not install, repair, configure, or start runtimes. |
| Runtime dependencies | `PASS` | Initial `office-pdf` / `fast-ocr` `not-installed/install` mismatch was fixed by read-only S3 installer status probes and state preservation of `missingModules` / `nextAction`. |
| Web UX/observability | `PASS_WITH_NOTES` | Typed action plans are available; richer inline rendering remains for S7. |
| Test/release | `PASS_WITH_NOTES` | S04 artifact now matches live snapshot and documents status-probe behavior. |

## Blocking Findings Resolved

- Public `/api/capabilities`, `/api/extensions`, and `agent_capability diagnose/list_packs` no longer expose raw `logPath`, `targetDir`, `configPath`, stdout/stderr, or output values.
- Capability packs without a fresh state file now merge S3 installer `status` facts before planning, so `office-pdf` and `fast-ocr` project as `missing_dependency` with `nextAction=repair`.
- OptionalAbilities state loading now preserves `missingModules`, `retryable`, `nextAction`, and repair/configure metadata.
- `agent_capability list_packs` and `diagnose` now use the same runtime capability service projection.

## Evidence

- `docs/web-runtime-goal/artifacts/S04-capability-service-tests.json`
- `python -m py_compile agent/runtime_capabilities.py agent/tools/optional_abilities/optional_abilities.py channel/web/web_channel.py agent/tools/agent_capability/agent_capability.py tests/test_web_runtime_goal.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_runtime_goal.py -k "s4_" -q`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "capabilities_api or agent_capability_safe_diagnostics or AgentCapabilityPermissions or extension_registry_projects_admin_capability_policy" -q`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_runtime_goal.py tests/test_v025_skill_tool_binding.py tests/test_v024_tongxin_cli_readonly.py -q`

# S3 Review Consensus: Runtime Packs And Installer

## Final Decision

`PASS_WITH_NOTES`

S3 passes after one fix/re-review loop. The initial security and runtime installer reviews found blocking issues; those were fixed, retested, and re-reviewed as non-blocking.

## Review Results

| Role | Agent | Initial Result | Final Result | Blocking Status |
| --- | --- | --- | --- | --- |
| Architecture consistency | Zeno | `PASS_WITH_NOTES` | `PASS_WITH_NOTES` | None |
| Security / permissions | Godel | `FAIL` | `PASS_WITH_NOTES` | Resolved |
| Runtime dependencies / installer | Aquinas | `FAIL` | `PASS_WITH_NOTES` | Resolved |
| Web UX / observability | Euler | `PASS_WITH_NOTES` | `PASS_WITH_NOTES` | None |
| Test / release | Arendt | `PASS_WITH_NOTES` | `PASS_WITH_NOTES` | None |

## Fixes From Failed Reviews

- Installer command logs, pip output, raised command errors, state messages, and status extras now redact credential-bearing URLs.
- State, target, and Playwright browser paths are confined to EcoreX owned state/runtime roots.
- Status/log/lock filenames use sanitized pack ids, preventing manifest id path traversal.
- Module probes no longer use host Python/global `sys.path`; they inspect only the capability target and owned runtime site-packages.
- Install/repair no longer falls back to host `sys.executable`; missing owned runtime Python returns `missing_runtime_python`.
- Install env is allowlisted and does not inherit prior `PYTHONPATH` or pip config.
- `configureOnly` packs now return `needs_configuration` and cannot pass as installed because `moduleChecks` is empty.
- Feishu/Lark manifest hints now prefer EcoreX owned Node/npm/npx and restrict system Node/npm/npx to full-access/admin diagnostic exceptions.
- Public runtime packs and generated runtime copies were mechanically synced after the fixes.

## Evidence

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_runtime_goal.py -k "s3_install_capability or s3_public_runtime_packs" -q` -> `9 passed, 10 deselected`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_runtime_goal.py tests/test_v025_tool_execution_environment.py tests/test_v024_tongxin_cli_readonly.py -q` -> `64 passed, 1 skipped`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_runtime_goal.py tests/test_v025_runtime_manifest.py tests/test_v025_runtime_dependencies.py tests/test_v025_tool_execution_environment.py tests/test_v024_tongxin_cli_readonly.py -q` -> `129 passed, 1 skipped`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/smoke-v023-install-packaging-contracts.py` -> `PASS`
- `bash -lc 'cd <WORKSPACE> && bash -n desktop/scripts/stage-runtime-mac.sh scripts/install-ecorex-web.sh scripts/check-ecorex-web-release.sh'` -> `PASS` with a non-fatal WSL localhost warning
- Web service release rebuilt: `release-artifacts/EcoreX_0.2.5-web-linux-service.tar.gz`
  - SHA256: `59C9E1979C751802A247565E2FF6D4F199F17F963DB967939B46218166C40E7B`
  - Size: `3,878,166`
- Real shared manifest handcheck: `tongxin-cli` reports `needs_configuration`, `installed=false`, `nextAction=configure`; doctor no longer counts it as installed.

## Non-Blocking Notes For Later Slices

- S4 should convert installer status into the final typed CapabilityService action plan, including `logRef`, `actionLabel`, `diagnosticSummary`, and a dedicated `needsConfiguration` summary count.
- S4/S9 should capture capability provenance so `installed` can distinguish target-package, owned-runtime preinstall, discovery-only, and configure-only states.
- S9 can further harden install subprocess `PATH` and explicit manifest provenance as a stricter release gate.
- The Web release tar contains runtime-pack-derived files at `runtime/capabilities.json` and `runtime/core-requirements.txt`, not a nested `runtime/runtime-packs/` directory.

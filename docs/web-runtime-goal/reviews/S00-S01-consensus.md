# S0/S1 Multi-Agent Consensus

## Scope

This consensus covers:

- `S0 Web Core Baseline`
- `S1 Config Safety`

Review was read-only. No reviewer edited files.

## Final Decision

`PASS_WITH_NOTES`

All five review perspectives returned `PASS_WITH_NOTES` with no blocking findings. The prior security `FAIL` was resolved by ignoring `user_datas.json` at root and nested paths, redacting runtime/state paths in artifacts, rejecting strict gates that include system PATH, and expanding regression coverage.

## Review Matrix

| Perspective | Agent | Verdict | Blocking Findings |
| --- | --- | --- | --- |
| Architecture consistency | Aristotle | `PASS_WITH_NOTES` | None |
| Security / permissions | Copernicus | `PASS_WITH_NOTES` | None |
| Runtime dependencies | Pasteur | `PASS_WITH_NOTES` | None |
| Web UX / observability | Gauss | `PASS_WITH_NOTES` | None |
| Test / release gate | Ohm | `PASS_WITH_NOTES` | None |

## Accepted Evidence

- `scripts/check-web-core-runtime-baseline.py` captures `coreRequired`, `optionalRepairable`, and `credentialRequired` dependency rows.
- `docs/web-runtime-goal/artifacts/S0-web-core-runtime-current.json` is redacted and records current owned-runtime gaps.
- `config.py` no longer uses `eval` for environment overrides.
- `config.py` writes `user_datas.json` and only imports legacy pickle data through a restricted, size-limited unpickler.
- `.gitignore` excludes root and nested `user_datas.json`.
- `docs/web-runtime-goal/artifacts/S01-config-safety-tests.json` records:
  - `tests/test_web_runtime_goal.py`: `7 passed`
  - compatibility runtime tests: `88 passed, 1 skipped`
  - strict gate rejects `--include-system-path`
  - JSON user data ignore rules verified

## Non-Blocking Notes To Carry Forward

- `defaultPreinstalled` is currently a defined category but has no emitted rows. S2/S3 should populate it from runtime-pack/manifest data instead of hardcoding a second list.
- `repairActionCounts` counts all rows with a `repairAction`, including ready rows. S4/S7 action-plan APIs must not present this field as "repairs needed".
- Strict Web release gates must remain owned-runtime only. General config still has host PATH convenience logic; S2/S3 must not allow that to contaminate release readiness.
- Credential probing currently checks environment variables. S4/S6 should resolve redacted credential source state from provider/admin/user configuration and return `needs_provider_credentials` instead of dependency failures.
- `save_user_datas()` writes JSON with default OS permissions. A later hardening slice should consider owner-only permissions and atomic writes.
- Add CLI-level release tests for `--strict --no-write` returning `1` on missing owned deps and default non-strict artifact writes.

## Consensus

No reviewer raised a blocking issue after fixes. S0 and S1 are accepted as the baseline for continuing into S2. Future slices must preserve the current constraints:

- Web-only scope.
- No desktop/Electron dependency.
- No additional state source.
- No prompt-driven dependency repair.
- No system PATH fallback in strict release gates.

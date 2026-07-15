# S03 Usage Panel Web Admin Surface

## Status

Completed.

## Intent

Expose the upgraded audit data at `/ecorex-agent/usage-panel/` with admin authentication.

## Decisions

- Reuse existing admin API data where possible.
- Do not add manual effective-artifact entry.

## Implementation

- Added `/ecorex-agent/usage-panel/` as an authenticated static alias for the existing Admin panel.
- Added `/ecorex-agent/usage-panel/api/*` as an Admin API alias.
- Reworked the runtime audit panel into business-oriented sections:
  - summary cards
  - user action categories
  - recent user actions
  - effective artifacts
  - thumbs-down feedback traces
  - collapsed technical event details
- Effective artifacts are read-only and derived from synced data.

## Verification

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q`

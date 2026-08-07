# Legacy v0.3 Runtime Packs

This directory is retained only for the v0.3 Web/desktop compatibility
installer. It is not an authority for the e-Mate v1 Runtime and must not be
read by new v1 capability, routing, admission, or release code.

The v1 authority chain is deliberately singular:

1. `ecorex/capabilities/builtin.py` owns signed `ToolSpec` contracts and routing.
2. `ecorex/pack_catalog.py` owns the exact Pack-to-tool/service mapping and the
   `minimal` / `full_offline` product profiles.
3. `release/capability-packs/` owns reviewed Pack source; the signed manifest
   frozen into a release slot is the executable fact.

`capabilities.json` and `core-requirements.txt` below describe only the legacy
installer. Copying an entry here does not install, enable, disclose, or
authorize a v1 capability.

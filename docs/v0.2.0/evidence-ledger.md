# v0.2.0 Evidence Ledger

| Date | Evidence | Status | Notes |
| --- | --- | --- | --- |
| 2026-06-23 | `git switch -c codex/ecorex-v0.2.0` | PASS | v0.2.0 work split from the v0.1.19 checkpoint branch. |
| 2026-06-23 | `npm version 0.2.0 --no-git-tag-version` from `desktop/` | PASS | Updated `desktop/package.json` and `desktop/package-lock.json` without creating a git tag. |
| 2026-06-23 | `python -m unittest tests.test_chat_channel_robustness` | PASS | CowAgent cancel regression restored: cancelling a session before futures are registered no longer raises `KeyError`. Existing ffmpeg/pydub environment warnings only. |
| 2026-06-23 | `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_chat_channel_robustness.py tests/test_ecorex_web_parallel_backend.py -k "stale_orphan_message_run or stale_live_message_lock or live_stale_session_lock or chat_channel_robustness or CancelSessionMissingFutures" -q` | PASS | 7 selected tests passed; verifies stale orphan active runs are terminalized and release backpressure while live stale locks are preserved. |
| 2026-06-23 | Parallel read-only agent A: discovery/channel/tool/knowledge | PASS | Found split discovery surfaces; recommended shared channel catalog, extension channel aggregation, `/api/channels` frontend merge, bridge allowlist fixes, and `/api/tools` `list_tools()` usage. |
| 2026-06-23 | Parallel read-only agent B: WebUI performance | PASS | Found structural render amplification around stream deltas, markdown parsing, token estimation, sidebar recomputation, and session switch/delete state fan-out. |
| 2026-06-23 | Parallel read-only agent C: install/update/project persistence | PASS | Found manifest promotion gap, early replace-mode UI-state overwrite risk, missing backend project truth, and installer smoke requirements. |
| 2026-06-23 | `python -m py_compile channel/channel_catalog.py agent/extensions/registry.py channel/web/web_channel.py` | PASS | Shared catalog, extension registry, and Web handlers compile after discovery fixes. |
| 2026-06-23 | `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_ecorex_web_parallel_backend.py -k "v020_channel_catalog or v020_extension_registry or v020_channels_handler or v020_tools_handler or v020_bridge" -q` | PASS | 5 selected tests passed; validates channel catalog parity/aliases, extension discovery without secret leakage, channel masking, MCP-aware tools list, and bridge/frontend discovery markers. |

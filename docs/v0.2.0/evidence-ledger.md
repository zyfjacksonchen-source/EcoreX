# v0.2.0 Evidence Ledger

| Date | Evidence | Status | Notes |
| --- | --- | --- | --- |
| 2026-06-23 | `git switch -c codex/ecorex-v0.2.0` | PASS | v0.2.0 work split from the v0.1.19 checkpoint branch. |
| 2026-06-23 | `npm version 0.2.0 --no-git-tag-version` from `desktop/` | PASS | Updated `desktop/package.json` and `desktop/package-lock.json` without creating a git tag. |
| 2026-06-23 | `python -m unittest tests.test_chat_channel_robustness` | PASS | CowAgent cancel regression restored: cancelling a session before futures are registered no longer raises `KeyError`. Existing ffmpeg/pydub environment warnings only. |
| 2026-06-23 | `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest tests/test_chat_channel_robustness.py tests/test_ecorex_web_parallel_backend.py -k "stale_orphan_message_run or stale_live_message_lock or live_stale_session_lock or chat_channel_robustness or CancelSessionMissingFutures" -q` | PASS | 7 selected tests passed; verifies stale orphan active runs are terminalized and release backpressure while live stale locks are preserved. |

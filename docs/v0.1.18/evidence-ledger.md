# EcoreX v0.1.18 Evidence Ledger

Status: Development Started / Production NO-GO
Branch: codex/ecorex-v0.1.18
Base commit: f8ff1db4 chore: stabilize EcoreX v0.1.17 gates

## Gate Rules

- Every code change must map to one or more `R18-*` acceptance rows.
- Every verification command must record command, result, and evidence path.
- v0.1.17 external release blockers remain visible but do not count as proof of
  v0.1.18 runtime completion.
- No credential, GitHub token, API key, signing secret, or user private path may
  be written into release evidence.

## Current Verification Runs

| Command | Result | Evidence |
| --- | --- | --- |
| `rg -l ... 'ghp_[A-Za-z0-9_]{20,}'` excluding build/vendor artifacts | PASS | No GitHub token pattern found in worktree before the v0.1.17 base commit |
| `git commit -m "chore: stabilize EcoreX v0.1.17 gates"` | PASS | Base commit `f8ff1db4` created before starting v0.1.18 |
| `python -m py_compile agent/protocol/run_ledger.py agent/protocol/__init__.py channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Run ledger module, WebChannel integration, and focused tests compile |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "run_ledger or active_request_snapshot or worker_completion or worker_exception or produce_exception or busy_session" -q` | PASS | 8 passed, 102 deselected; warnings limited to existing pydub/ffmpeg and `setDaemon` deprecations |

## Change Evidence

| Change ID | Area | Evidence Required | Current Evidence | Status |
| --- | --- | --- | --- | --- |
| R18-BOOT | Version boundary and tracking | Branch, goal, checklist, and evidence ledger exist before code changes | `docs/v0.1.18/goal.md`, `acceptance-checklist.md`, and this ledger created from base commit `f8ff1db4` | PASS |
| R18-RUN-LEDGER | Durable run/job state | Schema/module, write paths, active snapshot API, terminal exactly-once tests | Added `agent/protocol/run_ledger.py`; WebChannel creates runs, updates phase/terminal/cancel state, and `active_requests_snapshot()` prefers ledger; focused pytest covers durable active, terminal-once, registry fallback, worker finalize, produce exception, and busy-session interrupt | PARTIAL |
| R18-SSE-CONTRACT | Versioned stream events | Schema normalization, replay-gap behavior, terminal separation tests | Pending | TODO |
| R18-CANCEL-CONCURRENCY | Cancellation and backpressure | Cancel cascade tests, queue limits, subagent coordinator evidence | Pending | TODO |
| R18-MODEL-GATEWAY | Model call optimization | Capability catalog, telemetry, retry taxonomy, provider tests | Pending | TODO |

## Open Blockers

- v0.1.18 runtime implementation is just starting.
- v0.1.17 publication remains blocked by external release conditions recorded in
  `docs/v0.1.17/release-blockers-20260621.md`.

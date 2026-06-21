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
| `python -m py_compile channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | SSE contract backend and tests compile |
| `npm run typecheck` in `desktop` | PASS | StreamItem protocol fields compile in renderer and Electron TypeScript |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "sse or done_event or worker_exception or produce_exception or agent_stream_error or active_request_snapshot" -q` | PASS | 14 passed, 99 deselected; covers terminal error/cancel events, done-once, cursor resume, replay gap, and active snapshots |
| `python -m py_compile channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Cancellation snapshot slice compiles after backend active-state changes |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "active_snapshot_keeps_cancelling or active_request_snapshot or cancel_registry_snapshot or busy_session or sse_cancelled" -q` | PASS | 7 passed, 107 deselected; proves cancelling requests remain visible after SSE cancellation terminal while the cancel token is still registered |
| `python -m py_compile models/model_capabilities.py models/openai_compatible_bot.py models/chatgpt/chat_gpt_bot.py models/openai/open_ai_bot.py models/linkai/link_ai_bot.py bridge/agent_bridge.py channel/web/web_channel.py tests/test_model_capabilities.py tests/test_models_handler.py` | PASS | Model capability catalog, OpenAI-compatible sanitizer integration, AgentBridge routing, ModelsHandler, and focused tests compile |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_model_capabilities.py tests/test_models_handler.py -q` | PASS | 9 passed; covers provider inference, OpenAI fixed-sampling payload stripping, stream usage inclusion, custom-provider behavior, OpenAI-compatible bot integration, and `/api/models` chat capability exposure |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_qianfan_provider.py -q` | FAIL | 23 passed, 5 failed in existing docs/encoding assertions: missing localized qianfan docs, README qianfan snippet, and provider label text mismatch. Code-path subset below isolates the AgentBridge route touched by this slice |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_qianfan_provider.py -k "agent_bridge_routes_ernie_models_to_qianfan or bot_factory_returns_qianfan_bot or default_model_uses_ernie_when_model_is_provider_alias" -q` | PASS | 3 passed, 25 deselected; verifies AgentBridge provider inference still routes ERNIE/Qianfan code paths |
| `npm run typecheck` in `desktop` | PASS | Desktop TypeScript still compiles after `/api/models` chat capability adds the nested `capabilities` object |
| `git diff --check` | PASS | No whitespace errors in R18-04 model capability slice |
| `rg -l ... 'ghp_[A-Za-z0-9_]{20,}'` excluding build/vendor artifacts | PASS | No GitHub token pattern found before committing the R18-04 model capability slice |
| `python -m py_compile bridge/agent_bridge.py channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Web-owned cancel token owner slice compiles |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "web_request_token_survives or non_web_request_token or active_snapshot_keeps_cancelling or busy_session or worker_completion" -q` | PASS | 6 passed, 111 deselected; proves Web-owned request token survives AgentBridge success and error paths, `post_message()` marks Web ownership, non-Web token still self-cleans, and active/cancelling recovery paths remain covered |
| Multi-agent cross-review for Web-owned cancel token owner slice | PASS | Descartes agreed the lifecycle fix closes the original P0 risk; Boole found a P1 test gap, then confirmed it closed after adding error-path and real `post_message()` owner assertions; Lovelace agreed after the repair. Consensus: submit |
| `python -m py_compile channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Pre-worker abort cleanup helper, post_message integration, and focused tests compile |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "pre_worker_abort or compose_exception or filtered_context or thread_start_failure or active_request_snapshot or worker_completion" -q` | PASS | 7 passed, 113 deselected; covers filtered context, compose exception, and thread-start failure after request allocation, proving token unregister, SSE/request cleanup, lock release, and failed ledger terminal |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "pre_worker_abort or compose_exception or filtered_context or thread_start_failure or web_request_token_survives or non_web_request_token or active_snapshot_keeps_cancelling or busy_session or worker_completion or produce_exception" -q` | PASS | 10 passed, 110 deselected; verifies pre-worker cleanup does not regress Web-owned token, busy-session interrupt, worker completion, or produce-exception finalization |
| `git diff --check` | PASS | No whitespace errors in pre-worker abort cleanup slice |
| `rg -l ... 'ghp_[A-Za-z0-9_]{20,}'` excluding build/vendor artifacts | PASS | No GitHub token pattern found before committing the pre-worker abort cleanup slice |
| Multi-agent cross-review for pre-worker abort cleanup slice | PASS | Descartes agreed cleanup closes phantom token/lock/SSE/request risks; Boole found no P0/P1 and confirmed filtered/compose/thread-start tests cover the proof points; Lovelace agreed state-machine boundaries and terminal/lock semantics are safe. Consensus: submit |
| `python -m py_compile agent/protocol/cancel.py channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Cancel registry cancel-age fields, active snapshot merge path, and focused tests compile |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "cancel_registry_snapshot or active_snapshot_merges_registry_cancel_age or active_snapshot_keeps_cancelling or active_request_snapshot" -q` | PASS | 6 passed, 115 deselected; proves a long-running request cancelled at t+600 reports request age 602.5s but cancel age 2.5s, including ledger-preferred active rows |
| `npm run typecheck` in `desktop` | PASS | Renderer accepts `cancelled_at` and `cancel_age_seconds`; active request filtering compiles with cancel-age fallback |
| Multi-agent cross-review for cancel-age visibility slice | PASS | Descartes confirmed the prior P1 is closed by merging registry cancel metadata into ledger-preferred rows; Boole found no P0/P1 and agreed the new ledger merge test covers the masking path; Lovelace found frontend/runtime UX contract semantics consistent. Consensus: submit |
| `python -m py_compile channel/web/web_channel.py tests/test_ecorex_web_parallel_backend.py` | PASS | Typed busy/retry response backend path and focused tests compile |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "busy_session_message or active_snapshot_keeps_cancelling or active_request_snapshot" -q` | PASS | 6 passed, 116 deselected; proves rapid resend still interrupts when possible and the lock-still-busy fallback returns `REQUEST_CONFLICT_RETRYABLE` instead of raw `session_busy` |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "busy_session_message or web_request_token_survives or non_web_request_token or active_snapshot_keeps_cancelling or pre_worker_abort or worker_completion" -q` | PASS | 7 passed, 115 deselected; verifies typed busy fallback does not regress Web-owned token lifecycle, active cancelling recovery, pre-worker cleanup, or worker completion |
| `npm run typecheck` in `desktop` | PASS | Desktop accepts typed chat send error fields and compiles retryable conflict handling |
| Multi-agent cross-review for typed busy/retry contract slice | PASS | Descartes found no state-machine blocker and confirmed normal interrupt-then-acquire semantics are preserved; Boole found no P0/P1 and accepted the forced fallback test strategy; Lovelace agreed the frontend/runtime UX contract is consistent and does not auto-retry or hide the visible error. Consensus: submit |

## Change Evidence

| Change ID | Area | Evidence Required | Current Evidence | Status |
| --- | --- | --- | --- | --- |
| R18-BOOT | Version boundary and tracking | Branch, goal, checklist, and evidence ledger exist before code changes | `docs/v0.1.18/goal.md`, `acceptance-checklist.md`, and this ledger created from base commit `f8ff1db4` | PASS |
| R18-RUN-LEDGER | Durable run/job state | Schema/module, write paths, active snapshot API, terminal exactly-once tests | Added `agent/protocol/run_ledger.py`; WebChannel creates runs, updates phase/terminal/cancel state, and `active_requests_snapshot()` prefers ledger; focused pytest covers durable active, terminal-once, registry fallback, worker finalize, pre-worker abort, produce exception, and busy-session interrupt | PARTIAL |
| R18-SSE-CONTRACT | Versioned stream events | Schema normalization, replay-gap behavior, terminal separation tests | WebChannel normalizes SSE events with `protocol_version=ecorex.stream.v1`; worker/produce/agent errors emit `run.failed` error terminal instead of failure `done`; stream reconnect emits `stream.replay_gap` when cursor is older than replay window; focused pytest covers the contract | PARTIAL |
| R18-CANCEL-CONCURRENCY | Cancellation and backpressure | Cancel cascade tests, queue limits, subagent coordinator evidence | Active snapshot now reports cancel-registry `cancelling` state even after a cancellation SSE terminal has closed the UI stream; Web-owned request tokens remain registered until WebChannel finalization so `/cancel`, rapid resend, and active recovery can still see finishing requests; pre-worker abort cleanup prevents phantom active requests when `/message` fails before worker start; cancel snapshots and ledger-preferred active rows expose cancel-age so long-running requests do not disappear immediately after cancellation; busy fallback now returns a typed retryable conflict contract instead of raw `session_busy`; focused pytest covers these race windows | PARTIAL |
| R18-MODEL-GATEWAY | Model call optimization | Capability catalog, telemetry, retry taxonomy, provider tests | Added shared model capability catalog and OpenAI-compatible payload sanitizer; AgentBridge and `/api/models` chat capability now read shared provider/model capability resolution; focused tests cover sanitizer behavior and Qianfan route preservation. Telemetry/retry taxonomy still pending | PARTIAL |

## Open Blockers

- v0.1.18 runtime implementation is just starting.
- v0.1.17 publication remains blocked by external release conditions recorded in
  `docs/v0.1.17/release-blockers-20260621.md`.

# EcoreX v0.1.12 Hardening Completion Audit

Date: 2026-06-16

This audit maps the active hardening goal to current evidence. It is not a
completion claim. The goal remains active until user manual testing passes and
the requested GitHub/release/deploy sync is performed.

## Current Candidate

- Current candidate is `desktop/release-local-0023/win-unpacked` plus the
  regenerated Windows/macOS WebUI packages and public release ZIP.
- 0023 includes all 0021/0022 boundary fixes plus managed built-in workspace skill
  refresh, explicit `.ecorex-custom-override` preservation, and OpenAI image
  routing that keeps text-only/no-input-image creation on `/images/generations`
  while routing edit/reference/local input image or `image_url` requests to
  `/images/edits` multipart `image` / `image[]`. It also aligns the Admin
  image model catalog and auto hint to `gpt-image-2-pro`.
- Validation passed for the source/package set:
  `python -m unittest tests.test_ecorex_web_parallel_backend` (`79` tests),
  desktop typecheck/build/runtime staging, Electron dir packaging, WebUI/Linux
  packaging, public release ZIP generation, and release validator with desktop
  unpacked runtime.

- Latest local desktop hand-test package:
  `desktop/release-local-0023/win-unpacked/EcoreX.exe`.
- Latest desktop hand-test URL:
  `http://127.0.0.1:9899/app/`.
- Latest WebUI hand-test URL:
  `http://127.0.0.1:9909/app/`.
- Latest public release ZIP SHA256:
  `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`.
- Latest Windows WebUI ZIP SHA256:
  `2168D6F826221DBCD94BDC8F1F8CBC9C4E642A039C846E88E7C444866E9A19F2`.
- Latest macOS WebUI tarball SHA256:
  `CF3B4099B9B7425BA5A8EC976988BF0010E5A8BB75636B03B0AA90B81138AC24`.
- Anything before `release-local-0023` is stale for this hand-test cycle.

## Objective Requirement Audit

| Objective item | Current evidence level | Status |
| --- | --- | --- |
| Session summaries no longer show stale counts/recent-chat filler. | Renderer source no longer exposes per-row message-count summaries; release manifest records that rows no longer display `23 条`-style counts and stale cached pending bubbles normalize to paused. Browser smoke shows current rows as titles + timestamps. | Mostly proven; keep in manual visual checklist because cached production histories can still contain old titles. |
| Refresh/SSE reconnect and page/session switching do not break active work. | Backend has active request registry, request-aware cancel boundaries, replay/broadcast SSE logs, and renderer reconnect paths. Unit tests cover active requests, invalid request fallback, busy-session insertion, and same-request multi-subscriber SSE. | Code and unit-test proven for current-process cases; real long-task refresh remains manual-test dependent. |
| Dead session locks do not permanently block work. | SessionLock dead-PID/stale tests pass; worker completion/exception paths unregister cancel tokens and emit terminal SSE events. | Proven by tests for stale/dead owner and worker failure classes. |
| Xiaohongshu skill/Feishu read loops converge instead of paging forever. | Skill now requires bounded Feishu setup/auth flow and 5-12 selected references / max 3 pages; Agent Core routes raw Feishu shell to `feishu_cli` and enforces Feishu chain budget. | Host route proven; live user Feishu auth/document state still requires manual task validation. |
| Output that previously stopped halfway reaches terminal UI state. | Tool cancel/timeout, empty `agent_end`, worker crash, stream error, post-`done` tail, and text-only convergence tests pass. | Partially proven; a real provider half-stream reproduction is not yet deterministic. |
| WebUI/Desktop capability parity and UI parity. | API smoke on desktop `9899/app/` and WebUI `9909/app/` loads identical assets, dark theme defaults, matching API tools, active-request endpoints, and the same renderer bundle. Earlier browser visual smoke remains useful for screenshots but is not the latest package evidence. | API-level smoke proven; final UX proof remains user hand-test. |
| CDP remains first browser automation path. | `/api/tools` exposes browser description with CDP-first; config/packaging sentinels validate chrome-devtools `--browserUrl http://127.0.0.1:9222`. | Proven for packaged defaults and tool descriptions. |
| WebUI/Desktop same-device coexistence. | Runtime smoke ran desktop `9899` and WebUI `9909` simultaneously with version/API/static/tool/active-request checks; both returned empty active-request snapshots. | Proven for current local pair. |
| Codex-like host boundary alignment. | `host_diagnostics`, `feishu_cli`, global dangerous-tool permission broker, MCP namespace/isError handling, active requests, SSE replay, filesystem profile broker, memory/knowledge profile hooks, WebUI appdata-scoped permission state, and network/vision fail-closed hooks are present and tested. | Materially closer, but explicitly not Codex-equivalent; durable turn/process/replay/network/profile-UI/sub-agent gaps remain future work. |
| Rebuild formal hand-test artifacts and leave documentation. | `release-local-0023`, WebUI/Linux/public artifacts, validator output, release manifest, hardening log, completion audit, goal ledger, and packaging guidelines are updated. Download page browser smoke passed with `site.js?v=0.1.12-0023`, four final cards, and no broken images. | Proven for unsigned local candidates; signed Windows setup, GitHub push, and production deploy remain pending. |

## Proven By Current Evidence

| Requirement | Evidence |
| --- | --- |
| Session locks do not permanently block a session after a dead owner process. | `test_session_lock_removes_dead_owner_pid` and `test_session_lock_blocks_same_session_until_released` pass in `tests/test_ecorex_web_parallel_backend.py`. |
| SSE detach/reconnect does not cancel the backend request, and multiple subscribers do not steal events. | `test_multiple_sse_connections_receive_same_request_events` passes; WebChannel keeps replayable per-request SSE logs with per-subscriber cursors and keeps requests alive after client detach. |
| Active request lookup uses Web `request_id` for cancellation/interrupt boundaries. | `test_active_request_lookup_uses_web_request_id` passes. |
| Public/non-loopback WebUI bind cannot run passwordless. | `test_public_web_bind_requires_password` passes. |
| CDP/chrome-devtools defaults use the trusted `--browserUrl http://127.0.0.1:9222` path and are blocked when spoofed or read-only. | `test_chrome_devtools_mcp_startup_is_allowed_noninteractive` and `test_chrome_devtools_mcp_startup_rejects_spoof_and_read_only` pass. |
| Feishu/Xiaohongshu raw shell probing is routed toward the first-class Feishu tool and bounded. | `test_raw_lark_cli_bash_is_grouped_with_feishu_chain`, `test_simple_raw_lark_cli_bash_autoroutes_to_feishu_tool`, `test_raw_lark_cli_bash_autoroute_covers_npx_package_and_node_runner`, `test_complex_raw_lark_cli_bash_keeps_guidance_not_autoroute`, `test_feishu_tool_chain_budget_blocks_repeated_probing`, and `test_tool_chain_budget_forces_next_turn_text_only` pass. Packaged desktop/WebUI runtime smoke proved raw `npx @larksuite/cli...` autoroutes to `feishu_cli`; desktop package smoke also covered `node C:/cli-main/scripts/run.js ...` and `lark-cli auth login`. |
| Stuck tool/process classes have cancel/timeout boundaries. | Bash cancel/timeout, MCP cancellation/error, browser cancellation, MCP process/SSE deadline tests pass in the same suite. |
| Read-only/full-access permission modes reach the backend and write audit state. | Runtime smoke verified desktop `9899` and WebUI `9909`; `test_tool_permission_handler_round_trips_mode_and_audit` covers the WebChannel API handler. Packaged WebUI smoke also verified permission state follows `config.appdata_dir` when no env override is set. |
| Custom filesystem profiles constrain file tools and Web file serving. | `test_custom_filesystem_profile_limits_file_tools_to_workspace` and `test_web_file_serve_obeys_custom_filesystem_profile` pass. Packaged WebUI runtime smoke confirmed `read/write/ls/edit/send` allowed inside workspace, `.env` and outside paths denied, and `/api/file` returned `200` for an allowed file and `404` for a deny-glob `.env`. |
| Dangerous foreground/background host capabilities are inside the broker boundary. | Tests cover `env_config`, `send`, `scheduler`, `evolution_undo`, `web_fetch`, `web_search`, `vision`, `host_diagnostics` Feishu status, scheduler background execution, and scheduler persisted `tool_call`. |
| Dangerous tools cannot bypass the broker just because the channel is not Web/Desktop. | `test_non_web_channel_dangerous_tools_still_fail_closed` proves `bash`, `write`, `mcp_server`, and `web_fetch` are blocked in non-Web read-only/smart-ask modes and only allowed under explicit `full-access`. |
| Busy-session insertion, worker completion, pre-worker crash, and empty agent completion do not leave the UI permanently running. | `test_busy_session_message_interrupts_old_request_and_starts_new_one`, `test_empty_agent_end_emits_done_so_sse_does_not_hang`, `test_worker_completion_unregisters_cancel_token_but_keeps_sse_queue`, `test_worker_exception_emits_done_and_unregisters_cancel_token`, and `test_produce_exception_emits_done_and_unregisters_cancel_token` pass. |
| MCP tools cannot shadow first-party host tools, and Chrome DevTools MCP calls share browser/CDP convergence budget. | `test_mcp_tool_names_are_namespaced_and_remote_name_is_preserved`, `test_sync_mcp_into_agent_does_not_replace_builtin_tool`, and `test_chrome_devtools_mcp_calls_share_browser_chain_budget` pass. |
| Permission denial becomes a text-only convergence boundary instead of another tool-loop opportunity. | `test_permission_denial_forces_next_turn_text_only` and `test_forced_text_turn_sends_no_tool_schema_once` pass. |
| Tool runtime config merges refresh cached fields instead of only replacing `.config`. | `test_feishu_cli_apply_config_refreshes_cached_runtime_fields`, `test_feishu_cli_ensure_respects_auto_install_false`, `test_tool_manager_create_tool_applies_feishu_cli_config`, `test_agent_initializer_load_tools_applies_cached_tool_config`, and `test_host_diagnostics_apply_config_refreshes_cached_cwd` pass. |
| Skill load failures are visible to the model and diagnostics tools instead of only debug logs. | `test_skill_load_diagnostics_are_visible_in_prompt` and `test_host_diagnostics_reports_skill_load_diagnostics` pass; packaged runtime validation checks `format_skill_diagnostics_for_prompt`, `SkillLoader.last_diagnostics`, and `host_diagnostics._skill_status`. |
| Renderer can accept post-`done` media tail events and recover persisted audio from history. | `npm run build` passed; release validator confirms actual packaged `index.html` references `index-CjBkNLMl.js` and that the renderer bundle contains `voice_attach`, `extras?.audio`, and `/api/active-requests`. |
| Desktop host capability installation and external links have host-side boundaries. | Electron typecheck passes after adding capability-install permission checks, noninteractive preinstall gating, and main-process external URL scheme allowlisting. |
| WebUI and desktop can run simultaneously on one device without observed port or permission-state collision. | Runtime smoke: desktop `127.0.0.1:9899`, WebUI `127.0.0.1:9909`, both served `index-CjBkNLMl.js`; WebUI permission audit path resolved under its configured `state/appdata`, not the global fallback. |
| Desktop and WebUI can subscribe to the same running request without stealing each other's SSE events. | `test_multiple_sse_connections_receive_same_request_events` proves two EventSource generators for the same `request_id` both receive the same `done` event. WebChannel now uses `sse_events` replay logs and per-subscriber cursors instead of consuming a single shared Queue. |
| Current local hand-test packages are rebuilt after runtime changes. | `release-local-0023` desktop package and refreshed WebUI/Linux/public artifacts are recorded in `release-manifest.md`; release validator passed with `--desktop-dir desktop\release-local-0023\win-unpacked`, so Electron `app.asar`, packaged `resources/ecorex-runtime`, actual WebUI static references, active-request recovery sentinels, raw Feishu autoroute sentinels, same-request SSE broadcast sentinels, filesystem/memory/knowledge profile sentinels, appdata permission-state sentinel, network/vision fail-closed sentinels, and admin image model sentinels are covered. |

## Partially Proven Or Manual-Test Dependent

| Requirement | Current status |
| --- | --- |
| Page refresh after a long task resumes cleanly and replaces a stale pending bubble with persisted history. | Backend and renderer code exist; automated evidence covers SSE consumer replacement and invalid-request fallback paths indirectly. Needs browser/manual confirmation on a real long task. |
| Output that previously stopped halfway now reaches a terminal UI state. | Tool cancel/timeout and text-only convergence paths are covered. A real provider-stream half-output reproduction is not yet captured as a deterministic test. |
| Xiaohongshu skill no longer loops on the user's Feishu document/Base state. | Skill copy and Feishu tool-chain tests prove the intended host route and loop budget. A live Feishu task still depends on the user's current Feishu auth/document permissions and should be manually tested. |
| Desktop and WebUI can observe the exact same long provider/tool run at the same time in browsers. | Backend generator coverage proves broadcast behavior for terminal events. A browser-level long-task manual pass should still verify incremental deltas, tool rows, cancel, and reconnection across two real surfaces. |
| Web/Desktop visual parity under dark mode and compact header/sidebar sizing. | Browser visual/DOM smoke against desktop `9938/app/` and WebUI `9939/app/` loaded the same `index-CjBkNLMl.js` / `index-BG_69rJD.css`, defaulted to `data-theme=dark`, rendered the logged-in main app, exposed composer permission menu/token/context DOM, and opened the permission popover. Final proof remains user manual testing against the running desktop and WebUI surfaces. |

## Explicitly Not Complete Yet

- User manual testing of the current hand-test pair has not been confirmed.
- GitHub source push and release asset upload are intentionally paused until
  manual testing passes.
- Production download/Admin/Web deployment is intentionally paused until manual
  testing passes.
- Windows signed installer is not available yet because the SimplySign/Smart
  Card private-key provider is still unavailable to `signtool`.
- EcoreX is closer to Codex's host boundary but not equivalent. Remaining future
  work includes durable run registry, Feishu auth/page-flow state guards,
  managed process API, product-level sub-agent coordination, patch-first edit
  transactions, user-facing filesystem profile editing, and network sandbox
  profiles.

## Latest Local Evidence

- `python -m unittest tests.test_ecorex_web_parallel_backend` passed:
  `79` tests.
- `python -m py_compile common\ecorex_tool_permissions.py agent\memory\summarizer.py agent\knowledge\service.py agent\tools\web_fetch\web_fetch.py agent\tools\web_search\web_search.py agent\tools\vision\vision.py tests\test_ecorex_web_parallel_backend.py scripts\validate-ecorex-release-artifacts.py`
  passed.
- `npm run typecheck`, `npm run build`, `npm run stage:runtime:win`, local
  Electron dir build to `release-local-0023`, WebUI/Linux/public packaging
  scripts, and release artifact validator passed.
- `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0016\win-unpacked`
  is stale. The current validator pass is
  `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0023\win-unpacked`,
  including Electron `app.asar`, staged runtime host-boundary checks, and
  filesystem profile sentinels.
- Latest desktop hand-test path:
  `desktop/release-local-0023/win-unpacked/EcoreX.exe`.
- Latest WebUI hand-test URL:
  `http://127.0.0.1:9909/app/`.
- Browser smoke screenshots:
  `release-artifacts/manual-test-0016-browser-smoke/`.

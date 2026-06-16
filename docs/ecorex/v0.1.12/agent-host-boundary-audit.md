# EcoreX v0.1.12 Agent Host Boundary Audit

Date: 2026-06-15

## 2026-06-16 Latest Boundary Position

`release-local-0022` is now stale after the 0023 source delta. Parallel review
found one real boundary bug in the prior pass: the default filesystem fallback included
`web_file_serve_root`, and the default config points that at the user Home
directory. That made no-profile `smart-ask` file reads broader than intended.

0021, 0022, and 0023 source fixes:

- `_default_filesystem_profile()` now uses `agent_workspace`/cwd only. It does
  not treat `web_file_serve_root` or Home as a generic workspace root.
- `/api/file` defaults to workspace/upload preview roots and still calls
  `authorize_file_access("read", ...)`.
- Web log streaming reuses the host-diagnostics tail path for read permission
  checks and masking.
- Memory index sync, `MemoryService`, and `memory_get` now obey filesystem
  profiles before reading memory/knowledge files.
- `feishu_cli` `authRequired=true` and `available=false` results force a
  text-only next turn, so the agent must wait for user authorization/setup
  instead of continuing through raw shell probing.
- Official built-in workspace skill copies are refreshed from packaged
  `skills/` when they miss release-critical markers, so old
  `~/EcoreX/skills/image-generation` copies no longer mask the
  `gpt-image-2-pro` default or OpenAI endpoint fixes.
- OpenAI image-generation routing is now explicitly split: no input image means
  `/images/generations`; edit/reference/local input image or `image_url`
  requests mean `/images/edits` with multipart `image` / `image[]`.
- Admin/Models image capability now presents `gpt-image-2-pro` first for
  OpenAI and predicts it as the auto-mode default, so the settings UI matches
  the runtime default.

Validation passed with `79` backend tests plus the release validator against
`desktop/release-local-0023/win-unpacked`. Runtime smoke ran desktop
`127.0.0.1:9899` and installed WebUI `127.0.0.1:9909` simultaneously.

Current hand-test package is `release-local-0023`. Packages through
`release-local-0022` are stale.

The current `release-local-0023` rebuild confirms that EcoreX can align with
Codex at the practical policy/routing/current-process layer:

- Browser control is CDP-first and shares the same `127.0.0.1:9222` endpoint
  between the first-party browser tool and chrome-devtools MCP.
- Feishu/Lark work is routed through `feishu_cli`; simple raw shell calls now
  cover `lark-cli`, `npx lark-cli`, `npx @larksuite/cli...`, and local
  `node .../cli-main/scripts/run.js`.
- Host state is model-visible through `host_diagnostics`.
- Dangerous local capabilities fail closed outside explicit `full-access` or an
  interactive approval surface.
- File read/list/write/edit/send now share the same permission broker
  filesystem profile layer when an explicit profile is present. The first
  strict profile implementation supports workspace roots, read/write/deny
  access, deny glob rules, and `/api/file` serving checks.
- Background memory writes now use the same filesystem profile before creating
  `MEMORY.md`, daily memory files, or Deep Dream diary files. This closes a
  read-only/custom-profile bypass where automatic memory persistence could
  mutate disk outside explicit `write/edit` tools.
- Knowledge list/read/graph APIs now apply the same filesystem profile before
  reading `knowledge/*.md`, so custom deny rules also affect WebUI knowledge
  views.
- `web_fetch`, `web_search`, and `vision` now fail closed if the permission
  broker is unavailable instead of continuing with network/model upload work.
- WebUI permission state now follows the configured `appdata_dir` when no
  explicit user-data environment override is present. This prevents multiple
  extracted WebUI runtimes or a fresh one-click install from accidentally
  reading stale global `%LOCALAPPDATA%\EcoreX\permissions` state.
- Active request state and same-request SSE broadcast make desktop/WebUI
  coexistence usable while the same runtime process is alive.

It is still not Codex-equivalent. Do not claim full host parity until EcoreX
has durable turn/process APIs, managed PTY/process sessions, replayable event
logs after runtime restart, product-level sub-agent orchestration, patch/worktree
transactions, a UI/config surface for editable filesystem profiles, and network
sandbox profiles.

## 2026-06-16 Strict Codex Boundary Recheck

This pass rechecked the EcoreX Agent Core and host code against current Codex
public boundary concepts, using OpenAI Docs MCP because the Codex manual helper
returned HTTP 403 on the direct manual fetch. Relevant Codex source baseline:

- Codex separates agent work into surfaces such as thread/turn, sandbox,
  approval policy, permission profiles, filesystem permission, network policy,
  MCP, plugins, skills, browser use, computer use, subagents, worktrees, and
  automations.
- Codex permission profiles are least-privilege policies that combine
  filesystem and network rules. Built-in profiles include `:read-only`,
  `:workspace`, and `:danger-full-access`.
- Codex sandbox mode is the technical boundary for command execution; approval
  policy is the separate rule for when the user must approve an action.

Code-level result:

| Boundary area | Current EcoreX code evidence | Alignment result |
| --- | --- | --- |
| Dangerous tool approval | `common/ecorex_tool_permissions.py` now treats `bash`, `browser`, `feishu_cli`, `mcp`, `write`, `edit`, `skill_write`, `env_config`, `send`, scheduler, web, and vision tools as dangerous, with `full-access`, `read-only`, and interactive confirmation behavior. | Materially aligned at approval-policy intent. |
| File mutation | `write`, `edit`, and `SkillService` mutations check read-only/`skill_write`; skill package install validates names and zip extraction paths. `write` and `edit` now also call `authorize_file_access("write", ...)`. | Good current-runtime guard. |
| File read/list/send | `read`, `ls`, and `send` now call `authorize_file_access("read", ...)`. In `custom` mode without a filesystem profile, local file access fails closed. Explicit filesystem profiles support workspace roots and deny globs. Existing installs without a profile keep previous file read behavior. | First Codex-like filesystem profile layer landed. Still needs a user-facing profile editor and broader default policy presets. |
| Web file serving | `/api/file` remains authenticated/root-confined and now also calls `authorize_file_access("read", ...)` before serving local files. | Shares the same profile decision layer as agent file tools. |
| Network | `web_fetch`, `web_search`, and vision are gated as dangerous/read-only, but there is no domain allow/deny policy or local/private network guard. | Not Codex-equivalent. Needs network profiles. |
| Browser/CDP | `BrowserService` checks/auto-launches Chrome/Edge CDP and connects with `connect_over_cdp`; chrome-devtools MCP maps to the browser permission category. | Aligned for CDP-first routing. Missing richer browser/appshot/computer-use diagnostics. |
| MCP | MCP tool calls now propagate JSON-RPC and `isError=true` failures and map first-party chrome-devtools to browser permission, other MCP to `mcp`. | Partial. Needs per-server/per-tool approval annotations, resource status, and a stronger MCP admin surface. |
| Skills | Built-in and workspace skills are reloaded per prompt; custom workspace skills override built-ins; `SkillService` protects package install paths. | Good for workspace skill repair. Still prompt-discovery based, not a full plugin/skill lifecycle surface. |
| Process/terminal | `bash`, browser, and MCP paths have cancel/timeout handling. | Partial. There is no durable command/process session with stdin, output cursor, process tree status, and terminate API. |
| Run/session continuity | `/api/active-requests`, cancel registry, session locks, and same-request SSE replay exist while the runtime process is alive. | Good current-process liveness. Not durable across runtime restart or completed/expired replay. |
| Patch/worktree/subagent | Generic `write`/`edit`/`bash` exist. No patch-first transaction API, managed worktree, or product subagent coordinator exists. | Not Codex-equivalent. |

Conclusion: EcoreX can keep aligning with Codex at the product behavior layer,
but the correct target is "Codex-boundary-inspired", not "Codex-equivalent".
The next hard boundary work should be:

1. Add a user-facing profile editor/API for the new filesystem profile layer,
   including named presets and explicit secret deny rules.
2. Add a network profile layer for web tools and subprocess traffic, including
   public-domain allow/deny and explicit localhost/private-network handling.
3. Add durable command/process sessions with job ids, stdin, output cursors,
   process tree status, cancel/terminate, and audit logs.
4. Add patch/worktree-oriented file edit APIs before claiming Codex-like code
   editing semantics.
5. Add product-level subagent orchestration only after run/process state is
   durable enough to review and merge results.

Latest packaged evidence:

- Desktop: `desktop/release-local-0023/win-unpacked/EcoreX.exe`.
- Desktop smoke: `http://127.0.0.1:9899/app/`.
- WebUI smoke: `http://127.0.0.1:9909/app/`.
- Backend suite: `79` tests passed.
- Public release ZIP SHA256:
  `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`.
- Windows WebUI ZIP SHA256:
  `2168D6F826221DBCD94BDC8F1F8CBC9C4E642A039C846E88E7C444866E9A19F2`.
- macOS WebUI tarball SHA256:
  `CF3B4099B9B7425BA5A8EC976988BF0010E5A8BB75636B03B0AA90B81138AC24`.
- Release validator passed with
  `--desktop-dir desktop\release-local-0023\win-unpacked`.
- Packaged desktop and WebUI runtimes passed same-request SSE broadcast smoke
  and raw `npx @larksuite/cli...` -> `feishu_cli` autoroute smoke.
- Packaged WebUI runtime also passed direct permission and filesystem profile
  smoke: permission state resolved through `config.appdata_dir`; read-only
  blocked writes; full-access allowed writes; memory/knowledge paths obeyed
  custom deny rules; `knowledge/secret.md` was hidden from list/read.

This note records why EcoreX could get stuck in `bash` while Codex can often
modify a skill, inspect host state, or choose another route. The short version:
the model is only one layer. Codex exposes a larger structured host boundary;
EcoreX previously exposed many capabilities only as prompt text plus generic
shell access.

## Current EcoreX Boundary

| Layer | Current capability | v0.1.12 status |
| --- | --- | --- |
| Agent loop | `AgentStreamExecutor` supports multi-turn tool calls, streaming, cancellation checks, empty-output retry, and tool-result pairing. | Works, but loop protection previously only caught identical failed calls. v0.1.12 adds tool-chain convergence guards. |
| Tool registry | `ToolManager` loads built-in tools and dynamically loaded MCP tools. | Works. MCP tools now need the same permission boundary as first-party external tools. |
| Skills | `SkillManager` loads built-in and workspace `SKILL.md` files into prompt context. | Prompt-only by default. Skills do not become host abilities unless the relevant tool exists. |
| Files/shell | `read`, `write`, `edit`, `ls`, `bash` are model tools. | Available, but not equivalent to Codex host patch/worktree APIs. Dangerous tools go through the permission broker. |
| Browser | First-party `browser` tool uses CDP first, then configured fallback. | CDP remains first priority. chrome-devtools MCP now points at the same CDP endpoint with `--browserUrl`. |
| Feishu/Lark | `feishu_cli` wraps `lark-cli` for status, auth, install, and bounded command execution. | v0.1.12 adds packaging preinstall/fallback and makes the tool read `tools.feishu_cli` config. |
| MCP | stdio/SSE/streamable-http MCP servers can be loaded and exposed as tools. | v0.1.12 blocks noninteractive MCP startup unless allowed by permission mode, and maps MCP tool calls to `browser` or `mcp` permission categories. |
| Web/Desktop runtime | WebChannel provides `request_id`, SSE, cancel registry, session locks, and permission APIs. | Good for same-process reconnect, not durable process restart recovery. |
| Diagnostics | Logs existed, but the agent had no structured host-status tool. | v0.1.12 adds read-only `host_diagnostics` with sanitized runtime, CDP, MCP, permission, Feishu, and log tails. |

## Codex Boundary Gap

Codex App Server exposes structured thread, turn, command/process, skills,
plugin, MCP, config, and filesystem APIs. The public API overview explicitly
lists thread lifecycle/status, command/process execution and termination,
skills/plugin management, MCP server status/tool/resource calls, configuration
writes, and filesystem operations.

Source checked: https://developers.openai.com/codex/app-server#api-overview

EcoreX does not yet have the same host-level surface. The important gaps are:

- No durable run registry: `request_id`, SSE queues, and cancel tokens are mostly in memory.
- No replayable event log: a refreshed UI can reconnect while the process lives, but cannot replay a completed/expired stream by sequence.
- No native sub-agent coordinator: multiple sessions exist, but there is no product-level `spawn/join/review` agent API.
- No patch-first code edit API: `edit/write/bash` work, but there is no audited `apply_patch` protocol with git/worktree guardrails.
- No full sandbox profile enforcement: permission mode gates dangerous tools, but file/network boundaries are not yet equivalent to Codex sandbox profiles.
- MCP was historically outside the same permission boundary. v0.1.12 fixes startup/call authorization, but deeper sandboxing is future work.
- Tool/process state is not yet first-class: long-running commands do not have a durable job id, process tree, output cursor, and terminal state.

## Root Cause For Xiaohongshu/Feishu Stalls

The recent Feishu/Xiaohongshu stalls were not primarily because Feishu was bound
only to Codex. Local `lark-cli` was installed and authenticated. The product
failure was a boundary/convergence problem:

- The skill could read large Feishu pages and see `has_more=true`.
- The model changed offsets/filters, so identical-argument loop guards did not trigger.
- Some commands returned successful but unusable or huge output.
- EcoreX had no structured Feishu status tool or host diagnostic tool at first, so the model kept probing through `bash`.
- Codex tends to converge because its host surface has richer diagnostics, stricter tool budgets, better skill/plugin boundaries, and stronger operator instructions.

v0.1.12 addresses this by making `feishu_cli` the preferred path, adding
small-page selection rules to the skill, adding `select_feishu_references.py`,
and adding tool-chain convergence guards in the agent loop.

## v0.1.12 Fixes Landed

- Added `agent/tools/host_diagnostics` and exposed it through `ToolManager`.
- Added `agent/tools/feishu_cli` and packaged Windows `lark-cli.exe` when available.
- Added Feishu CLI install fallback to Windows/macOS WebUI one-click installers.
- Added tool-chain convergence guards for repeated Feishu, browser/CDP, and shell chains.
- Changed chrome-devtools MCP defaults from `--autoConnect` to `--browserUrl http://127.0.0.1:9222 --no-usage-statistics`.
- Made MCP stdio/SSE/streamable-http startup obey noninteractive permission checks.
- Mapped chrome-devtools MCP tool calls to the `browser` permission category and other MCP calls to `mcp`.
- Changed dangerous-tool permission failures to fail closed instead of silently allowing execution.
- Extended config/log masking to include nested `token`, `password`, and `authorization` values.
- Made `FeishuCli` read `tools.feishu_cli.package` and `tools.feishu_cli.auto_install`.
- Strengthened the main prompt host-boundary rules so `host_diagnostics`,
  `feishu_cli`, and CDP-first browser control are visible in Chinese and
  English sessions.
- Added pre-execution routing so simple raw `bash lark-cli ...` commands run
  through `feishu_cli`; complex raw Feishu shell and raw shell CDP probing are
  stopped before execution and redirected to the dedicated host tools.
- Tool short-circuit paths now emit start/end events, so loop-budget stops,
  permission denial, tool-not-found, and reroutes show up as concrete UI tool
  rows instead of leaving the user with a hanging thinking state.
- Default `chrome-devtools` MCP startup is allowed through noninteractive
  authorization, while actual browser automation remains behind the normal
  `browser` permission category.
- `host_diagnostics` now reports whether self-evolution is enabled; config
  defaults align with the release template.

## Remaining Boundary Truth

The v0.1.12 hardening makes EcoreX much less likely to loop blindly, but it is
not yet a full Codex host clone. Do not claim these are complete until
implemented and verified:

- Managed process/terminal jobs with durable ids, stdin, output cursors, and
  process tree status.
- Product-level sub-agent orchestration with spawn/join/review contracts.
- Patch-first file editing with worktree safety semantics equivalent to Codex.
- Durable replayable run logs across runtime process restart.
- Full filesystem/network sandbox profiles.

## Codex Alignment Conclusion

EcoreX can align with Codex at the policy and routing layer in v0.1.12, but not
yet at the full host boundary layer.

Now aligned or materially closer:

- Browser automation policy: CDP remains the first path, and the packaged
  chrome-devtools MCP server points at the same `127.0.0.1:9222` browser.
- External service routing: Feishu work is routed to `feishu_cli` instead of
  repeated raw shell probing.
- Model-visible diagnostics: `host_diagnostics` gives the agent structured
  host state before it guesses.
- Permission intent: `full-access`, `read-only`, and interactive confirmation
  are visible in the UI and enforced by the backend permission broker for local
  high-risk tools.
- UI terminal truth: short-circuited tools now emit concrete start/end events,
  so blocked/rerouted/denied tool calls do not leave the frontend in a fake
  thinking state.

Still not Codex-equivalent:

- Codex has first-class thread/turn APIs, `turn/steer`, `turn/interrupt`,
  command/process sessions with stdin/output/terminate, app-server filesystem
  operations, skills/plugins/config APIs, and MCP status/tool/resource APIs.
  EcoreX still relies on WebChannel request ids, SSE, in-memory cancel tokens,
  generic file/shell tools, and local runtime config.
- Codex can self-correct a skill or route more often because the host tells it
  what capabilities exist and exposes managed APIs for changing course. EcoreX
  can now steer away from known bad loops, but it still needs future durable job
  state, replayable event logs, a process API, patch-first edit semantics, and
  stronger sandbox profiles to fully match that boundary.
- Source note: the comparison above is based on the official Codex app-server
  API overview, which lists thread/turn, command/process, filesystem,
  skills/plugins/config, and MCP surfaces:
  https://developers.openai.com/codex/app-server#api-overview

## 2026-06-15 Runtime Convergence Follow-Up

The second pass found one remaining reason EcoreX could still behave less like
Codex: loop handling was mostly prompt-level. The executor could tell the model
"stop repeating this chain", but the next LLM request still exposed the full
tool schema, so a stubborn model could select another tool call instead of
closing the turn.

v0.1.12 now adds a narrow host-level circuit breaker in
`AgentStreamExecutor`:

- When repeated identical failures hit the retry budget, the next model turn is
  forced to text-only by withholding all tool schemas once.
- When an external capability chain budget is exhausted (`feishu_cli`,
  browser/CDP, or generic shell), the next model turn is forced to text-only.
- When the same tool succeeds repeatedly with identical arguments, the next
  turn is also forced to text-only so the assistant must summarize the gathered
  result instead of continuing to probe.

This does not make EcoreX a full Codex host clone, but it moves loop closure
from "model please cooperate" to a real runtime boundary. Simple raw
`lark-cli ...` shell commands are now automatically routed into `feishu_cli`
so the current turn can keep using the bounded Feishu host path. Complex shell
forms with pipes, redirects, or command separators remain hard-stopped instead
of being reinterpreted as trusted Feishu CLI arguments.

Skill self-repair alignment is also clarified: EcoreX should not mutate packaged
built-in skills in place. If a built-in skill causes a structural loop, the
Codex-aligned path is to create a same-name workspace skill copy, patch that
workspace override, and let `SkillManager` precedence select the fixed copy.
This uses the existing custom-over-builtin skill loading rule while keeping the
release package immutable.

## 2026-06-15 Subprocess, MCP, And Skill Boundary Follow-Up

The next hardening pass moved several remaining failure modes from "model has
to notice" into deterministic host behavior:

- `AgentStreamExecutor` now attaches the request cancel event to every tool
  instance before execution.
- `bash` no longer waits inside a single blocking `communicate()` call. It
  polls the child process, honors cancel events, and kills the process tree on
  cancel or timeout.
- `feishu_cli` uses the same cancel-aware subprocess loop for packaged
  install/ensure/auth/run flows. This directly addresses the user-visible
  symptom where a Xiaohongshu/Feishu task could remain in a fake thinking
  state while a child process was stuck.
- `feishu_cli` binary discovery includes the packaged CLI root
  `C:\EcoreX Artifact Desk\cli-main` and checks Windows `.exe` / `.cmd`
  variants, so an installed CLI can be found without raw shell probing.
- MCP `tools/call` failures are no longer returned as successful strings.
  JSON-RPC `error` and MCP `isError=true` now raise into `McpTool`, which
  returns `ToolResult.error`. This gives the executor a real failure signal and
  prevents the model from treating broken MCP calls as useful evidence.
- MCP `tools/list` JSON-RPC failures now raise instead of returning an empty
  tool list. `ToolManager` can therefore mark the server `failed` instead of
  `ready` with zero tools.
- MCP stdio tool calls now poll cancellation and shut down the server process
  on cancel or timeout. SSE/streamable-http calls check cancellation before and
  after blocking network work.
- Browser operations now poll the active request cancel event while waiting for
  the background Playwright thread. On cancel/timeout the waiting request
  returns immediately and marks the browser service for restart on the next
  call.
- `read-only` permission mode now covers file mutation tools (`write`, `edit`,
  `fs_write`) and skill mutations (`skill_write`), not only shell/browser/MCP
  style external capabilities.
- `SkillService` now validates skill names, validates downloaded file paths,
  and safely extracts zip packages without allowing `..`, absolute paths, or
  zip-slip writes outside the workspace skill root.
- `SkillService` rejects Windows reserved names, trailing-dot names, silent
  overwrite of existing custom skills, and silent workspace overlays of built-in
  skills unless the caller explicitly sets the appropriate replace/override
  flags.
- Startup builtin-skill sync is initialize-only. Existing workspace skill
  overlays are never deleted at launch, so a same-name skill repair survives
  restart instead of being overwritten by the packaged copy.
- The bundled `create-xiaohongshu-note` skill now defines a finite Feishu
  setup/auth flow: `status` once, `ensure` once if missing, `auth_login` once if
  authorization is missing, then stop and wait for user authorization instead
  of looping through raw `lark-cli` via `bash`.

These fixes explain the Codex/EcoreX behavior difference in concrete terms:
Codex exposes managed process/skill/file/turn controls that make interruption
and self-repair first-class host operations. EcoreX is not fully there yet, but
v0.1.12 now has hard host signals for the highest-impact cases instead of
depending on prompt obedience alone.

## Release Gate

Before publishing v0.1.12 assets, rebuild all formal packages. Any package hash
recorded before the host-boundary changes is stale.

Minimum checks:

- `python -m py_compile` for changed Python runtime files.
- `python tests\test_ecorex_web_parallel_backend.py` must pass and include the
  host-boundary regressions for text-only convergence, MCP error semantics,
  MCP cancellation, browser cancellation, SkillService path/overwrite safety,
  read-only file/skill write blocking, and bash cancellation.
- `npm run typecheck` and `npm run build` for desktop/WebUI.
- Verify `/api/tools` includes `host_diagnostics` and `feishu_cli`.
- Verify `host_diagnostics(action=status)` reports Feishu availability/auth and CDP readiness or a clear CDP error.
- Verify `read-only` blocks `bash`, `browser`, `feishu_cli`, MCP startup/tool
  calls, local `write`/`edit`, and skill add/delete/enable/disable.
- Verify `full-access` allows the same tools and writes permission audit records.
- Verify chrome-devtools MCP config uses `--browserUrl`, not `--autoConnect`.
- Verify Windows WebUI package contains `runtime/tools/bin/lark-cli.exe` when a Windows CLI binary is available.
- Verify macOS package either contains `runtime/tools/bin/lark-cli` from `ECOREX_LARK_CLI_DARWIN` or logs the npm fallback clearly.

## 2026-06-16 Agent Core Boundary Extension

Parallel explorer review found that the remaining gap was not only "can the
foreground chat ask for permission?" but "can every model-visible or background
host capability reach the same broker?" v0.1.12 now extends the boundary:

- `env_config`, `send`, `scheduler`, `evolution_undo`, `web_fetch`,
  `web_search`, and `vision` are classified as dangerous host capabilities in
  `common/ecorex_tool_permissions.py`.
- The foreground executor fails closed for that same set if the permission
  broker itself fails.
- Direct tool invocation is guarded for read-only mode:
  - `env_config set/delete` cannot mutate environment config.
  - `send` cannot expose local files.
  - `scheduler create/delete/enable/disable` cannot mutate durable background
    task state.
  - `evolution_undo` cannot restore memory/skill files.
  - `web_fetch`, `web_search`, and `vision` cannot perform internet/model API
    access or local download/upload side effects.
- Scheduler background execution now requires noninteractive `scheduler`
  authorization. Persisted `tool_call` actions additionally authorize the
  concrete target tool, so a hand-edited `tasks.json` cannot use scheduler as a
  bypass for `bash`, `web_fetch`, `vision`, browser, MCP, or other dangerous
  tools.
- `host_diagnostics` remains a read-only diagnostic surface, but its Feishu
  status probe now checks noninteractive `feishu_cli` permission before it can
  run `lark-cli auth status`.

This still is not a complete Codex-equivalent sandbox. EcoreX now has a broader
permission broker and more fail-closed host gates, but it does not yet provide
Codex-style per-run filesystem sandboxes, network policy profiles, patch-based
edit transactions, or a managed sub-agent/process API. Those remain explicit
future work.

Regression coverage added in `tests/test_ecorex_web_parallel_backend.py`:

- smart-ask requires permission for `env_config`, `send`, `scheduler`,
  `evolution_undo`, `web_fetch`, `web_search`, and `vision`.
- read-only blocks direct `send`, `env_config`, scheduler mutation,
  `evolution_undo`, `web_fetch`, `web_search`, and `vision`.
- scheduler background execution fails closed without noninteractive
  permission.
- scheduler persisted `tool_call` checks the target tool permission before
  execution.
- `host_diagnostics` does not launch Feishu CLI status when noninteractive
  `feishu_cli` permission is denied.
- `/api/tool-permissions` handler round-trips permission mode changes through
  the same persisted broker state and writes a permission audit file. This
  catches UI/API regressions where WebUI or desktop appears to switch modes but
  the backend permission boundary is not actually updated.

## Next Work After v0.1.12

- Durable run registry with request/session/job status.
- Replayable SSE/event log with `event_seq` or `Last-Event-ID`.
- Managed process API with process tree kill, stdin, output cursor, and terminal state.
- Product-level sub-agent coordinator for explorer/reviewer/worker roles.
- Patch-first edit tool with git/worktree safety rules.
- Stronger sandbox profiles for filesystem and network access.
- Skill/plugin source governance, version locks, and dependency install policy.

## 2026-06-16 Global Broker And Desktop Host Boundary Follow-Up

Parallel review found that the broker was still scoped too narrowly: dangerous
tools were only forced through the permission broker when `ECOREX_DESKTOP=1` or
when `channel_type` contained `web`. That meant non-Web daemon channels could
silently treat `bash`, `write`, `mcp_server`, `web_fetch`, and similar tools as
`not-required`.

Fixes:

- `ToolPermissionBroker._requires_permission()` now classifies dangerous tools
  globally, independent of channel type.
- Foreground `authorize()` still allows `full-access` and remembered grants, and
  still blocks `read-only`. When a runtime has no interactive Web/Desktop
  approval surface, `smart-ask`, `always-ask`, and `custom` fail closed
  immediately instead of waiting for a permission UI that cannot appear.
- Added `test_non_web_channel_dangerous_tools_still_fail_closed`.
- Desktop optional capability installation now passes through the Electron
  permission manager before running package installers. Background preinstall
  has no interactive UI, so it only proceeds when the local permission state
  allows it noninteractively.
- Desktop `window.open` and the application menu now use a main-process external
  URL allowlist. Only `https:` and `mailto:` are opened with
  `shell.openExternal`; `file:`, `javascript:`, `ms-*`, and custom protocols
  are rejected before they reach the OS.

Validation:

- `python tests\test_ecorex_web_parallel_backend.py` passed (`41` tests).
- `npm run typecheck` passed after the Electron permission changes.
- `npm run build`, `npm run stage:runtime:win`, local Electron dir build,
  WebUI/Linux/public release packaging, and release artifact validation passed
  after this follow-up.

## 2026-06-16 SSE Finalization And MCP Shadowing Follow-Up

Parallel review found two additional Codex-boundary gaps:

- Request lifetime was not fully host-owned. WebChannel registered cancel
  tokens before execution but did not unregister them on normal worker
  completion, and a synchronous `produce(context)` crash could leave SSE open
  with keepalives until idle timeout. This could make a finished or failed task
  look like it was still running.
- MCP tools used remote names directly. A malicious or accidental MCP server
  could expose `bash`, `browser`, `feishu_cli`, or `host_diagnostics` and shadow
  EcoreX's first-party host tools.

Fixes:

- WebChannel now finalizes every request after worker completion, worker
  exception, or pre-worker produce exception. Finalization unregisters the
  cancel token, preserves the SSE queue until terminal consumption, emits a
  terminal error `done` event for crash paths, and releases the session lock.
- MCP tools are exposed to the model as `mcp__<server>__<tool>` while preserving
  the remote tool name internally for MCP RPC calls.
- Dynamic MCP sync refuses to replace non-MCP first-party tools.
- Permission denial now forces the next model turn to text-only, so the model
  must explain the blocker or request authorization instead of repeatedly
  calling the denied tool.
- Chrome DevTools MCP tools share the `browser:cdp` chain budget.

Validation:

- `python tests\test_ecorex_web_parallel_backend.py` passed (`48` tests).
- `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0011\win-unpacked`
  passed. The validator now checks packaged WebUI/Linux/Desktop runtime source
  text for the request finalization and MCP namespace invariants.

## 2026-06-16 Config And Diagnostics Closure Follow-Up

Parallel review after the MCP/SSE rebuild found three remaining reasons EcoreX
could still fail to self-correct where Codex usually converges:

- Some tools cached host config in constructor fields. Updating `tool.config`
  through ToolManager or AgentInitializer did not necessarily update the fields
  used by execution. `FeishuCli` could show an updated package/auto-install
  config while still behaving as if defaults were active.
- Skill load failures were model-invisible. A malformed or missing-description
  skill was only a debug log, so the agent could not reason that a skill was
  unavailable because the file was invalid.
- The renderer treated `done` as a hard end to the request even though the
  backend can intentionally emit post-done tail events such as `voice_attach`.

Fixes:

- `BaseTool.apply_config()` is now the standard config-refresh hook.
  `FeishuCli` and `HostDiagnostics` override it to refresh cached runtime
  fields, and ToolManager/AgentInitializer call it after config merges.
- `feishu_cli action=ensure` respects `auto_install=false` and explicit
  `install_if_missing=false`.
- `SkillLoader`, `SkillManager`, and `host_diagnostics` expose recent skill
  load diagnostics to the model-visible prompt and diagnostic tool output.
- The renderer accepts `voice_attach` after `done`, maps persisted
  `extras.audio` to media steps, refreshes cached sessions from runtime
  history, closes streams on `cancelled`, and keeps `/uploads/...` as runtime
  HTTP media.
- Release validation now checks actual packaged static references and public
  download-file parity, preventing packages where the new bundle exists but
  `index.html` still points at an old hash.

Validation:

- `python tests\test_ecorex_web_parallel_backend.py` passed (`55` tests).
- `npm run typecheck`, `npm run build`, and `npm run stage:runtime:win` passed.
- WebUI/Linux/public packages were rebuilt and validated.
- `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0013\win-unpacked`
  passed.

Boundary note: this narrows the practical self-correction gap with Codex by
making more host state visible and enforceable. It is still not full Codex
parity because EcoreX does not yet have product-level sub-agents, durable run
registry, replayable event log, managed process API, or Codex-style sandbox
profiles.

## 2026-06-16 Active Request Runtime Boundary Follow-Up

The latest audit compared EcoreX against the Codex app-server runtime-status
surface. Codex exposes stored thread reads/lists with runtime status, active
thread notifications, turn start/steer/interrupt, command/process sessions, fs
APIs, skills/plugins/config APIs, and MCP status/resource/tool APIs. EcoreX does
not yet have that full app-server model, but v0.1.12 now closes the biggest
practical refresh/switch gap for the current WebUI/Desktop product.

Fixes:

- `CancelTokenRegistry` now stores request creation time and exposes
  `snapshot()` with non-content runtime metadata.
- WebChannel exposes `/api/active-requests`, using the cancel registry as the
  backend source of truth and adding the current SSE availability flag.
- `loadRuntimeSnapshot()` fetches active backend requests together with sessions,
  tools, skills, models, and version state.
- The renderer marks session rows running from backend truth, not only cached
  frontend bubbles.
- Selecting/restoring a session can reconnect to a backend-reported
  `request_id`, creating a placeholder assistant bubble if the cached pending
  bubble was lost.
- Backend request rows now carry `state=running|cancelling`, `cancelled`,
  `created_at`, `age_seconds`, and `stream_available`. This lets the UI show a
  true stopping state instead of hiding a cancelled request that is still
  unwinding.
- Active requests are synthesized into the session list even when the session is
  not in the newest `/api/sessions?page_size=40` page and no local cached bubble
  exists.
- After repeated SSE reconnect failures, the renderer checks
  `/api/active-requests` before pausing a bubble. If the backend still owns the
  request, the UI keeps waiting or reconnects; if `stream_available=false`, it
  falls back to history refresh instead of opening a known-invalid EventSource.
- History recovery now treats file/media/tool-step-only assistant completions as
  final when there is no text body, which matters for generated images and
  attachment-heavy skills.

This prevents a common false state: after refresh, another tab, or short SSE
disconnect, the UI should no longer decide that a task is stopped merely because
local cache disappeared. The backend runtime decides whether a request is still
active.

Still not Codex-equivalent:

- No durable event replay after the EcoreX runtime process exits.
- No explicit thread/turn object model equivalent to Codex app-server.
- No `turn/steer`; same-session new input still uses interrupt/restart semantics.
- No managed PTY/process API with stdin, resize, output cursors, and terminate.
- No app-server filesystem/sandbox API.

Validation:

- `python tests\test_ecorex_web_parallel_backend.py` passed (`58` tests).
- `cd desktop && npm run typecheck` passed.

Remaining risk: this is still in-process runtime truth, not persistent Codex
rollout/event replay. If the Python runtime exits, EcoreX can recover from
persisted conversation history but cannot replay terminal/process output by
cursor. A future Codex-parity pass needs a durable run table, event sequence,
bounded SSE queues, and a managed process API.

## 2026-06-16 Agent Core Boundary Conclusion

The direct code audit found that EcoreX can be made much closer to Codex at the
policy, visibility, and loop-closure layers, but it cannot honestly be called
Codex-equivalent without adding new host infrastructure.

Current EcoreX Agent Core shape:

- `AgentStreamExecutor` owns the multi-turn model/tool loop, retry behavior,
  cancellation checks, tool-result pairing, chain budgets, and one-turn
  text-only circuit breakers.
- `ToolManager` dynamically loads first-party tools and MCP tools, now
  namespacing MCP tool names to avoid first-party shadowing.
- Local file, shell, browser, Feishu, network, vision, scheduler, skill, and MCP
  operations are exposed as model tools guarded by `ToolPermissionBroker`.
- `BrowserTool` uses CDP first through Playwright's CDP client and can
  auto-launch Chrome/Edge at `127.0.0.1:9222` before falling back when allowed.
- `feishu_cli` is the supported Lark path; simple raw `bash lark-cli ...`
  calls, `npx @larksuite/cli...`, and local `node .../cli-main/scripts/run.js`
  calls are pre-execution autorouted into `feishu_cli`, while complex shell
  forms still receive a hard-stop guidance message instead of another shell
  opportunity.
- `host_diagnostics` gives the model sanitized runtime, permission, CDP, MCP,
  Feishu, skill, and log state before it guesses through shell.
- Web/Desktop task liveness is now backend-authoritative through
  `/api/active-requests`.
- Same-request SSE is now replay/broadcast based: multiple EventSource
  subscribers read from per-request event logs with independent cursors instead
  of consuming a single shared Queue, so desktop and WebUI can observe the same
  request without stealing each other's events.

Still not Codex-equivalent:

- No OS/container filesystem sandbox profile comparable to Codex sandbox modes.
  EcoreX has permission decisions and tool-level guards, not kernel/process
  isolation.
- No durable command/process sessions with stdin, PTY resize, output cursors,
  and process kill APIs equivalent to Codex `command/exec` and `process/spawn`.
- No first-class `turn/steer`; same-session user insertion still maps to
  interrupt/restart behavior rather than steering the active turn.
- No product-level sub-agent API with spawn/join/review contracts. Parallel
  review exists in this development environment, not inside EcoreX runtime.
- No patch-first edit transaction API with worktree safety semantics. EcoreX
  still relies on generic `write`, `edit`, and `bash`.
- No durable cross-process replay of every SSE item or tool output.
- `custom` permission mode is still a named mode without a complete
  user-editable policy language equivalent to Codex config policy.

Practical conclusion: v0.1.12 should be described as "Codex-boundary inspired
hardening", not "Codex host parity". It now gives the agent enough host truth to
avoid the observed bash/Feishu/CDP loops and false-running UI states, but full
parity requires a new durable host service layer.

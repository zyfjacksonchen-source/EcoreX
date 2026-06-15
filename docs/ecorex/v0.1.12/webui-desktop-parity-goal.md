# EcoreX v0.1.12 WebUI/Desktop Parity Goal

Date: 2026-06-15

This document records the follow-up fixes after local WebUI testing. Keep this
with the v0.1.12 release notes so future packaging work does not regress the
same areas.

## Required behavior

- GitHub upload must happen only after formal local WebUI and desktop release
  builds pass the regression checks below.
- WebUI and desktop must use the same React renderer and the same Python
  runtime behavior for browser tools, shell tools, file preview, rich text, and
  media display.
- WebUI and desktop must default to dark mode on first launch. Dark-mode text
  color must stay high-contrast and Codex-like, not muted gray.
- The composer must expose a Codex-style local-access selector as one upward
  menu with `Full access`, `Smart ask`, `Always ask`, `Read-only`, and `Custom`
  choices. `Full access` must map to the same local permission boundary as
  Codex full access for local shell/browser/file tools.
- CDP browser control is the first browser automation path on both WebUI and
  desktop. The runtime should attach to or auto-launch Chrome/Edge at
  `http://127.0.0.1:9222` before falling back to Playwright-managed Chromium.
- Session rows must not show message-count summaries such as `23 items`.
- The context meter must include user/assistant text, tool inputs/results,
  reasoning/phase content, file references, and images. It must not use only
  raw message text length.
- Chat content must render Markdown-rich text, tool disclosures, local file
  links, and local images consistently in WebUI and desktop.
- Sending a new message while the same session is running must interrupt the
  active run and submit the new message, matching the desktop behavior.
- Switching sessions, refreshing the page, or temporarily detaching SSE while a
  task is active must not stop the backend request. The renderer must persist
  the assistant message `request_id` and reconnect to the same stream. Explicit
  Stop and same-session interrupt sends are the only normal cancellation paths.
- If the whole local runtime exits and the cached `request_id` becomes invalid,
  the stale pending bubble must normalize to a paused state and require a new
  user message, rather than staying in `thinking` or showing raw transport
  errors.
- The chat composer must not have a horizontal divider above it in either WebUI
  or desktop.
- Chat bubbles must expose one-click text copy in WebUI and desktop.
- Desktop and WebUI can run at the same time on one device without port,
  workspace, session-lock, or installation-manifest conflicts.
- Scrollbars must follow the active light/dark theme.
- Long-running shell commands must respect timeout boundaries and must not leave
  orphaned Windows PowerShell child processes that keep the UI in a permanent
  "thinking" state.

## Findings

- Installed WebUI `config.json` was intentionally minimal and did not contain
  `tools.browser`, so the runtime never entered the CDP-first path even though
  source defaults documented it.
- The browser tool imports without Playwright, but execution failed before CDP
  could be used because the Playwright Python package was absent from the core
  WebUI runtime. CDP needs the Playwright client package, but it does not need
  `playwright install chromium`.
- The stuck system-disk optimization run was not a pure frontend spinner bug.
  Logs showed recursive PowerShell size scans where `timeout=120` surfaced after
  373 seconds because Windows `subprocess.run(shell=True)` timed out the wrapper
  process while child PowerShell work could continue holding pipes.
- Session list detail text came from `mapSessions()` formatting
  `session.msg_count` as a message count, so the fix belongs in the shared React
  source, not in generated WebUI assets.
- Markdown rendering already existed, but local `file://` and absolute-path
  image links were not converted to the WebUI `/api/file` preview endpoint.
- WebUI `/message` returned `session_busy` as soon as `SessionLock` was held,
  while desktop users expected a new send to interrupt the current run.
- Tool events were matched by tool name only. A repeated `bash` or `browser`
  call could end the wrong disclosure row, leaving a later row still marked
  running even after the backend finished.
- SSE disconnects previously closed the browser/EventSource side without a
  durable request identity. If the UI missed the terminal event, cached pending
  messages could reappear as a permanent thinking session after relaunch, or a
  page/session switch could accidentally detach from a request that was still
  running.
- The permission broker blocked for up to 300 seconds on tool approval without
  a visible composer-level mode control. `full-access` needed to be a real
  backend mode and a visible WebUI/desktop setting.
- The shared chat bubble did not expose a direct copy button for WebUI users,
  making web-side text reuse slower than desktop expectations.
- The formal desktop sidecar could run on `9899` while WebUI ran on `9924`,
  but the desktop default workspace and installation surface still looked like
  legacy `~/cow`/`webui`, making same-device state harder to reason about.
- Browser default scrollbars stayed visually light in dark mode.
- First-load theme used system/light fallback when no saved preference existed,
  so new installs could start in light mode even when the release expectation
  was dark by default.
- The context meter underestimated active sessions because it ignored tool
  arguments/results, intermediate content, media/file references, and CJK token
  density.

## Fixes Made

- `config.py` now fills runtime-critical EcoreX defaults after loading a minimal
  `config.json`: `tools.browser` CDP settings and `chrome-devtools` MCP.
- Win/Mac WebUI install scripts now generate `config.json` with the same
  browser defaults instead of relying only on runtime repair.
- `playwright` is included in the core runtime requirements as the CDP client.
  The `browser-automation` capability pack remains responsible for installing
  Playwright-managed Chromium fallback.
- The browser tool description and capability text now state CDP-first behavior.
- Windows `bash` tool execution now starts commands in a killable process group
  and uses `taskkill /T /F` on timeout to terminate child PowerShell processes.
- Session rows show project name or "recent conversation", not message counts.
- Cached pending messages are normalized to a non-running paused state so old
  localStorage state cannot keep a session permanently marked as running.
- WebUI and desktop default to dark mode at first paint. Electron uses a dark
  initial window background/titlebar, and the web document carries
  `data-theme="dark"` before React mounts.
- Dark theme text is raised to high-contrast white, while muted text remains
  secondary.
- The composer-level permission selector is now one upward menu, aligned with
  the token/context meters in the same footer row. It switches `full-access`,
  `smart-ask`, `always-ask`, `read-only`, and `custom`. Settings also includes
  the full permission mode list.
- The backend permission broker supports `full-access`, `smart-ask`,
  `always-ask`, `read-only`, and `custom`; `full-access` lets local shell and
  browser tools proceed without approval prompts, while read-only blocks
  dangerous local tools.
- SSE `tool_start` and `tool_end` events include `tool_call_id`, and the React
  renderer matches tool disclosures by call id before falling back to name.
- SSE client detach keeps the backend request alive and preserves the queue so
  a later `/stream?request_id=...` can resume. React caches live assistant
  `requestId` values and reattaches when the session/page is restored. A stale
  invalid request id from a fully stopped runtime is converted to paused.
- The chat composer top divider was removed in shared CSS; WebUI and desktop
  both use the same divider-free composer zone.
- The context meter now estimates text, CJK characters, structured tool data,
  file attachments, and image/video references.
- Markdown local file/image URLs are converted through `/api/file?path=...`, so
  WebUI and desktop can display local images directly in chat.
- WebUI busy-session sends now cancel the active session request, push a
  `cancelled` SSE event to the old bubble, wait briefly for the session lock,
  and then submit the new message.
- The shared React chat bubble now has an icon-only copy action for message
  text, with Clipboard API and textarea fallback.
- Desktop runtime defaults now use the EcoreX workspace when the config still
  contains the legacy `~/cow` placeholder, memory storage follows
  `agent_workspace`, legacy conversation rows are migrated without overwriting
  new data, and runtime installation registration uses `desktop` when
  `ECOREX_DESKTOP=1`.
- Global scrollbar styling now uses the current theme variables and hides
  default scrollbar arrow buttons.

## Regression Checks

- Verify `/api/tools` contains `browser`.
- Verify a minimal installed `config.json` without `tools.browser` still results
  in CDP defaults in memory after startup.
- Verify `browser(action=navigate, url=https://example.com)` auto-launches or
  attaches to Chrome/Edge CDP before any Chromium fallback.
- Verify `bash` timeout on Windows returns near the requested timeout and does
  not leave child `powershell.exe` processes running.
- Verify the session sidebar no longer displays message counts.
- Verify first launch starts in dark mode on WebUI and desktop, with readable
  white primary text.
- Verify the composer shows one upward local-access menu and switching
  `Full access`, `Smart ask`, `Always ask`, `Read-only`, and `Custom` updates
  `/api/tool-permissions` on WebUI and desktop.
- Verify full-access shell/browser runs do not wait on the old approval prompt,
  while read-only still blocks dangerous write/execute tools.
- Verify the context meter grows when tool output and attached files/images are
  present.
- Verify Markdown text, local file links, and local images render in both WebUI
  and desktop.
- Verify sending a new message during an active WebUI run interrupts the old
  bubble and starts the new request instead of returning `session_busy`.
- Verify switching sessions, refreshing the page, and reopening the WebUI page
  during an active task reattaches with the original `request_id` and continues
  receiving results.
- Verify a stale cached request from a fully stopped runtime reopens with the
  assistant bubble marked paused, not thinking.
- Verify the chat composer has no top divider line in WebUI or desktop.
- Verify repeated same-name tool calls (`bash`, `browser`) do not leave stale
  running tool rows after their `tool_end` events arrive.
- Verify message text copy works in WebUI at `http://127.0.0.1:<port>/chat`.
- Verify formal desktop `9899` and local WebUI `9924` can run together and
  report separate `desktop`/`webui` installation surfaces under the same
  workspace manifest.
- Verify dark mode scrollbars use dark track/thumb colors.

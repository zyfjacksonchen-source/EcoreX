# EcoreX v0.2.1 Web Goal

## Objective

EcoreX v0.2.1 delivers only the Web surface: `channel/web`, browser WebUI, Web-facing Admin API routes, and deployment under `https://mvdcm.ecoremedia.net/ecorex-agent/`. Desktop shell, Electron packaging, desktop-only settings, and installer UX are paused for this version.

The release goal is to make the agent feel Codex-like in the browser: stable long-running turns, observable work, clean final answers, compact reasoning/work panels, named subagents, usable memory/knowledge/channels, and channel capability readiness after authorization.

## Scope

- Fix duplicate final conclusions and keep final assistant content out of call-process rows.
- Render final Markdown answers and short/medium live Markdown; keep long streams cheap.
- Add one-click copy for answers, Markdown, platform copy, Xiaohongshu copy, images, files, and artifacts.
- Stop stale recovery cards from staying at the bottom of the chat or pulling new messages above an interrupted turn.
- Reduce false disconnects through typed SSE heartbeat, transient reconnect grace, tool heartbeat, adaptive tool leases, and terminal-only recovery prompts.
- Treat image generation and other silent long tools as first-class long-running tasks instead of letting bash default timeouts kill them.
- Replace noisy long call-process output with a WorkBuddy-like compact thinking/work box.
- Add named subagent lifecycle events, timeouts, heartbeat/deadline metadata, slot release, and Web card/tree visualization.
- Restore CowAgent-like Memory, Knowledge Graph, and Channels in the Web settings/management path.
- Keep Feishu/Lark IM bot channel distinct from the `feishu_cli` user connector, and refresh capability cache after channel authorization.
- Built-in `browser-automation`/browser capability path so users do not hit `capability installer not found`.
- Add EcoreX-colored indeterminate activity status indicators with shimmer/sheen animation for live work and reconnect states.

## Non-Goals

- No Electron shell changes unless required by the shared Web renderer.
- No desktop installer/package acceptance gate.
- No desktop-only settings page validation.
- No Feishu/Lark real closed-loop conversation-call acceptance in v0.2.1; user moved that validation to v0.2.2.
- No secret material in release logs or docs.

## Acceptance Gates

- Final assistant answer appears once; call-process rows do not duplicate the conclusion.
- Markdown renders as formatted content in history and delivery, not raw `# Markdown`.
- EventSource reconnects and tool silence do not show a recovery card unless the backend confirms terminal/interrupted or unrecoverable status.
- New messages remain in chronological turn order after reconnect/recovery.
- Project sessions remain under their project folder and do not drift into general sessions.
- Image-generation/long-tool turns receive visible heartbeat/deadline feedback and can extend lease before timeout.
- Tool timeout is terminal, visible, and frees the run/subagent slot.
- Artifact `...` menus close on outside click, Escape, and focus cleanup.
- Subagents have user-readable names, summaries, expected outputs, status, timeout/deadline, preview, stop/collect/diagnostic controls, and no bare `subagent #9` UI.
- Memory/knowledge graph/channel pages are available in Web and remain performant through lazy loading/scale limits.
- Channel connect/disconnect updates status/error fields and refreshes capabilities; real Feishu/Lark conversation-call validation is deferred to v0.2.2 by user direction.
- Browser automation is discoverable as a built-in capability, with diagnostics if a process restart/tool reload is required.
- Web-only production deployment passes local runtime and public proxy smoke on the new domain path.

## Multi-Agent Review Rule

Implementation must be reviewed by independent agents across UI/state, runtime/reconnect, channels/Feishu, subagent orchestration, deploy, and observability. Findings are recorded in `review-log.md`; PASS requires no remaining v0.2.1 P0/P1 blockers.

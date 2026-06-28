# v0.2.1 Review Log

## Review Protocol

The implementation writer does not self-review for final PASS. Review findings below came from independent parallel agents and were incorporated into source changes or tracked as blockers/future work.

## 2026-06-24 Initial Multi-Agent Review

| Agent | Slice | Result | Findings Applied |
| --- | --- | --- | --- |
| Runtime/state reviewer | SSE/history/recovery/project ownership | PASS-WITH-FIXES | Stable turn identity, `done.final_text`, heartbeat, terminal-only recovery, and merge-safe recovery card removal. |
| UI/UX reviewer | Markdown/copy/artifact/thinking space | PASS-WITH-FIXES | Markdown rendering, copy actions, artifact menu outside-click/Escape, compact work box, and shimmer on live states. |
| Subagent reviewer | Naming, timeout, orchestration | PASS-WITH-FIXES | Metadata, heartbeat/deadline, terminal timeout, slot release, active children snapshots, and Web card/tree events. |
| Channel/Feishu reviewer | Channels and post-auth capability discovery | PASS-WITH-DEFERRED | Catalog/status/capability refresh applied. Real Feishu Web call is deferred to v0.2.2 by user direction. |
| Deploy reviewer | New path/domain and Web-only routes | PASS-WITH-FIXES | `/ecorex-agent/client/*`, `/assets/*`, `/message`, `/upload`, stream buffering, and timeout config added. |

## 2026-06-24 Disconnect Deep-Dive Round

| Agent | Slice | Result | Findings Applied |
| --- | --- | --- | --- |
| Lagrange | Tool lease and image-generation disconnects | PASS-WITH-FIXES | Tool heartbeat/deadline/timeout added; bash max raised; long-command default timeout added for image/render/build/install commands. |
| Hooke | WorkBuddy-style Feishu/channel connection | PASS-WITH-FIXES | Channel connect marks capability refresh required; Feishu bot channel and `feishu_cli` connector kept separate. |
| Ramanujan | WorkBuddy "never disconnects" behavior | PASS-WITH-FIXES | Preserve EventSource native reconnect, avoid rendering heartbeat, use grace window before recovery card. |
| Meitner | Browser automation and deploy route gaps | PASS-WITH-FIXES | Browser automation built-in path added; `/client/*` route fixed for Web-only deployment. |

## 2026-06-24 Observability Round

| Agent | Slice | Result | Findings Applied |
| --- | --- | --- | --- |
| Avicenna | Codex-like session observability | PASS-WITH-FUTURE | v0.2.1 keeps Run Center visible and active request snapshots; v0.2.2 should add session `runtimeState`, `hydrationState`, `actionSummary`, and `notLoaded` states. |
| Pascal | Durable run/lease model | PASS-WITH-FUTURE | v0.2.1 adds heartbeat/deadline events; v0.2.2 should add schema-versioned durable event ledger and lease manager. |
| Nash | Shimmer placement and motion design | PASS-WITH-FIXES | Shimmer limited to current phase/tool/reconnect indicators, not whole session rows or thinking ring; reduced-motion guard added. |
| Popper | Agent autonomy and observation context | PASS-WITH-FUTURE | v0.2.1 exposes observable facts; v0.2.2 should inject observation context so the agent can decide longer timeouts/retries from live telemetry. |

## Remaining Notes

- Feishu/Lark real authorization and Web conversation call have not been completed in this run because they are now v0.2.2 scope.
- Full interactive browser smoke under `https://mvdcm.ecoremedia.net/ecorex-agent/` is still recommended for artifact copy, reconnect ordering, subagent timeout, memory/graph, and channel settings; production Web/API/Admin smoke has passed.
- Manual browser smoke for artifact copy, reconnect order, image long task, subagent timeout, memory reader, graph click, and channel settings remains pending.

## Future Hardening Not Blocking v0.2.1 Source Merge

- Durable append-only run event ledger with replay/compaction.
- Global active-session observable stream across sessions, including `active`, `notLoaded`, `reconnecting`, `waiting_permission`, `tool_running`, `subagent_running`, and `timeout`.
- Capability epoch/reload so newly installed browser automation tools become visible without process restart where possible.
- Agent observation-context injection that summarizes heartbeat age, lease, deadline, last tool output, pending permission, and suggested next action.
- Adaptive per-tool timeout policies owned by a lease manager instead of regex/source-local heuristics.

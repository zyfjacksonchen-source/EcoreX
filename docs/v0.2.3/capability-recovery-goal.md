# EcoreX v0.2.3 Capability Recovery Goal

## Objective

Restore EcoreX from a degraded "chat-only" user perception by making v0.2.2 first-party tools and v0.2.3 CDP/Feishu additions discoverable, visible, and testable through the runtime API surfaces before repackaging and redeploying.

## Scope

- Preserve v0.2.2 basic capabilities: `bash`, file read/write/edit/list/find, host diagnostics, memory, scheduler tool surface, runtime projection, and skill discovery.
- Preserve v0.2.3 additions: CDP-first browser, Chrome DevTools MCP discovery, Fast OCR, Feishu/Lark `feishu_cli`, External Connections projection, and optional ability install gates.
- Do not weaken permission gates: discovery may be visible while execution still follows existing permission policy.
- Do not mutate sealed v0.2.2 release artifacts or hashes.

## Root Cause

- First-party tools were present in `ToolManager`, but the unified extension surface did not expose them as first-class entries.
- Cold-start `ExtensionRegistry` and channel-agent snapshots could read an empty `ToolManager` singleton without self-loading built-in tool classes.
- The settings ability panel collapsed many states into "待配置", making loaded built-in tools look like unconfigured external packs.

## Fix Plan

- Make extension/channel discovery self-load built-in tools when the `ToolManager` snapshot is empty.
- Add `builtin_tool` entries for first-party tools to `/api/extensions`.
- Keep `/api/tools` and real tool execution as the source of truth for tool schema availability.
- Update the abilities panel to show `已加载`, `未加载`, `等待刷新`, `CDP 优先`, or `需凭据` instead of a blanket "待配置".
- Add `scripts/smoke-v023-capability-recovery.py` and include its clean artifact in the final release gate.

## Review Standard

Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression must all agree before PASS.

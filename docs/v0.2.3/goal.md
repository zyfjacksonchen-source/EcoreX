# EcoreX v0.2.3 Goal

## Objective

Implement the v0.2.3 external-connection and browser/OCR reliability goal:

- Add Settings > External Connections / 外部连接 as a first-level settings module for Hermes-style messaging-platform management.
- Make CDP the first-priority, out-of-the-box browser automation path, with auto-launch on first use and Playwright persistent fallback.
- Enable the upstream Chrome DevTools MCP full-compatible toolset and bundled agent skills on top of EcoreX's CDP-first browser path.
- Add a fast OCR/URL path so screenshot/link extraction can hand recognized links directly to browser/CDP.
- Add an EcoreX-native self-learning skill draft/register path inspired by Hermes `/learn`, and remove the fixed built-in `create-xiaohongshu-note` skill.
- Insert a performance optimization slice for long-running and complex-task slowness, without rolling back any capability.
- Insert a conversation identity/sorting integrity slice to fix project/general cross-talk, disappearing pinned sessions, rename-triggered pinning, and Codex-like session ordering.
- Insert a Codex-like user attachment chat bubble slice so text+file+image messages are compact and readable without the current oversized orange container.
- Preserve v0.2.2 backend-led runtime invariants: durable runtime events remain canonical, frontend state is projection-driven, and Run Center stays hidden from ordinary users.
- Preserve the v0.2.3 regression lessons in `docs/v0.2.3/regression-pitfalls.md` and use them as guardrails for future slices.

## Non-Negotiable Constraints

- Do not directly port Hermes Gateway queues, sessions, or delivery runtime.
- Do not create a second frontend-owned source of truth for channel/connection state.
- Do not persist raw secrets, raw OCR text, cookies, tokens, or complete browser profiles in public runtime events.
- Do not let agents write formal skills directly with generic file tools; learned skills must pass draft validation, security review, role review, approval, and `SkillService` registration.
- Do not "optimize" by hiding events, removing tool capabilities, skipping projection replay, disabling OCR/CDP/image jobs/scheduler/subagents, or weakening security scans.
- Do not let rename, title lock, pin/unpin, local UI state hydration, or list pagination change session ownership, message ownership, or request/session binding.
- Missing real credentials can only be marked blocked or simulated, never real PASS.
- Each slice needs multi-angle review before release PASS: Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, Release/Regression.
- Future feature slices must check the regression pitfall guardrails before promotion, especially tool discovery, frontend status labels, production runtime API smoke, sealed v0.2.2 boundaries, optional capability separation, install/package bloat, CDP/OCR handoff, session identity/sorting, long-run performance, attachment replay, privacy scans, and fail-closed final gates.

## Slices

- R23-00: v0.2.3 documentation and baseline audit.
- R23-01: CDP/OCR failure baseline.
- R23-02: CDP-first browser defaults and fallback behavior.
- R23-02C: Chrome DevTools MCP full-compatible toolset and bundled upstream skills.
- R23-03: BrowserAutomationService diagnostics consolidation.
- R23-04: Browser/CDP tool selection priority.
- R23-05: Fast OCR / URL OCR tool path.
- R23-06: OCR-to-CDP link intake handoff.
- R23-07: External Connections backend contract.
- R23-08: External connection actions API.
- R23-09: Messaging adapter contract.
- R23-10: Runtime event integration.
- R23-11: Settings > External Connections frontend.
- R23-12: Hermes-style configuration wizard.
- R23-13: Feishu/Lark first real adapter.
- R23-14: Existing platform metadata rollout.
- R23-15: Scheduler/home-channel delivery.
- R23-16: Security and permission review.
- R23-16P: Performance optimization for long-running and complex-task slowness; must pass before final release gate.
- R23-18: Hermes skill-learning research and EcoreX-native draft/register design.
- R23-19: Remove fixed `create-xiaohongshu-note` built-in skill and route future vertical workflows through self-learning skill creation.
- R23-20: Conversation identity, project/general isolation, pin/rename semantics, and Codex-like sorting integrity; must pass before final release gate.
- R23-21: Codex-like user attachment chat bubble layout for text+file+image messages.
- R23-17: Final regression and release gate.

## Current Baseline

- `tools.browser.cdp_endpoint` already defaults to `http://127.0.0.1:9222`.
- `tools.browser.cdp_auto_launch` was false in the default config, which made the documented CDP-first behavior fail when Chrome was not manually launched.
- `vision` is a high-quality image understanding tool with a long model timeout; it is too slow for URL-only screenshot extraction.
- Image jobs already have OCR reuse telemetry, but that path is scoped to image generation/editing, not quick link extraction.
- `skill-creator` already exists as a generic built-in authoring skill; v0.2.3 should build a controlled runtime draft/register layer around it rather than keep a giant hard-coded Xiaohongshu skill.
- A 2026-06-26 user screenshot shows session cross-talk symptoms after a pinned image-to-image skill conversation and rename-triggered pinning. Initial code audit confirms `desktop/src/App.tsx` currently pins on rename and merges backend sessions, local UI state, and active requests without a single canonical session ownership resolver.
- A 2026-06-26 user screenshot shows text+PPT+image messages are visually too heavy: the entire user payload is wrapped in a strong orange container and attachment cards are oversized. R23-21 narrows this to a frontend transcript layout fix.
- Upstream `chrome-devtools-mcp` exposes advanced DevTools, network, performance, memory, and agent-skill workflows. R23-02C enables the compatible set through EcoreX's localhost CDP endpoint while keeping extension/pipe-only surfaces explicit.

# EcoreX v0.2.3 Regression Pitfalls and Guardrails

This document records the pitfalls hit during v0.2.3 so future feature work
does not reintroduce the same regressions. Treat it as a release-gate companion
to `acceptance-checklist.md`, `development-log.md`, and `review-log.md`.

## 1. Built-In Tools Can Exist but Still Be Undiscoverable

- Symptom: Settings shows built-in abilities as `待配置`; even `bash`/shell looks unavailable; EcoreX feels like a plain chat AI.
- Root cause: `ToolManager` could load tools, and `Bash` could execute, but cold-start surfaces such as `/api/extensions` and `/api/channels` read an empty `ToolManager` without self-loading.
- Guardrail:
  - Any API/projection that reports tool availability must call `ToolManager.load_tools()` when the singleton is empty.
  - First-party tools must be exposed as `builtin_tool` extension entries, for example `tool:bash`, `tool:browser`, `tool:feishu_cli`, and `tool:ocr`.
  - Release gates must check `ToolManager`, `/api/tools`, `/api/extensions`, channel agent surfaces, and at least one real command execution.

## 2. Do Not Collapse Distinct Ability States into `待配置`

- Symptom: Ready built-in tools, credential-gated external platforms, permission-gated tools, and not-yet-installed optional packs all look identical.
- Root cause: Frontend cards treated missing config, missing schema, runtime pending refresh, permission gating, and service not running as one state.
- Guardrail:
  - Backend projection remains the source of truth for readiness.
  - Frontend must render separate labels for loaded, not loaded, pending refresh, permission gated, credential missing, auth required, CDP first, and service running.
  - OCR availability must accept the dedicated `ocr` tool independently from `vision`.

## 3. Package and Manifest Checks Are Not Enough

- Symptom: Deployment appears successful because package hashes and public manifest are correct, but the live service can still miss tools.
- Root cause: Public HTTP smoke only proved download artifacts and `/api/version`, not runtime API capability surfaces.
- Guardrail:
  - Production rollout must include a server-side authenticated smoke for `/api/tools`, `/api/extensions`, `/api/channels`, and `/api/external-connections`.
  - Production smoke must assert core tools, v0.2.3 tools, built-in `tool:*` extension entries, and Feishu schema visibility.
  - Final release gate must require production deploy, public HTTP smoke, production capability smoke, and privacy scans.

## 4. v0.2.2 Is Sealed; Do Not "Fix" It by Mutating Sealed Evidence

- Symptom: Running old v0.2.2 release-gate tests in a v0.2.3 workspace reports historical hash/artifact blockers.
- Root cause: v0.2.2 artifacts are sealed while the workspace now contains v0.2.3 release artifacts.
- Guardrail:
  - Verify v0.2.2 invariants with focused capability/regression subsets.
  - Do not rewrite sealed v0.2.2 hashes or artifacts to make mixed-version gates green.
  - Record boundary notes when sealed-gate checks are intentionally not promoted.

## 5. Optional Capabilities Must Not Hide Core Capabilities

- Symptom: Optional packs or MCP/CDP install state makes core tools appear unavailable.
- Root cause: Capability policy and frontend status were treated as if every ability must be configured or installed before discovery.
- Guardrail:
  - Built-in tools are always discoverable if shipped.
  - Optional pack state may affect install/enable actions, not first-party tool schema visibility.
  - Permission and credential gates must be visible as gates, not as absence.

## 6. Feishu Has Two Surfaces and They Must Stay Separate

- Symptom: Feishu message channel readiness and `feishu_cli` tooling readiness are conflated.
- Root cause: External connection platform state and agent tool state share naming but have different auth, credentials, and runtime semantics.
- Guardrail:
  - `feishu` message channel uses app credentials and channel projection.
  - `feishu_cli` is an agent tool/optional ability with explicit status and auth actions.
  - Missing channel credentials must be blocked honestly; visible `feishu_cli` schema is not proof that the message channel is configured.

## 7. Installation Must Not Block on Heavy Optional Dependencies

- Symptom: Windows installation stalls at Feishu environment setup; macOS package size grows sharply.
- Root cause: Optional SDK/OCR/runtime dependencies were bundled or installed synchronously as if required for the base app.
- Guardrail:
  - Base install should ship core tools and metadata, but heavy SDKs remain optional or on-demand unless required for startup.
  - Windows first-run install must not synchronously install Feishu SDK before the app is usable.
  - macOS package audits must watch wheelhouse/runtime bloat for RapidOCR, OpenCV, and Lark SDK entries.

## 8. CDP First Must Be Open-Box, Not a Manual Setup Prompt

- Symptom: Browser tasks fail with connection refused and tell the user to manually start Chrome.
- Root cause: CDP was preferred in intent but not auto-launched or diagnosed consistently.
- Guardrail:
  - Desktop browser automation defaults to CDP first, auto-launch on first use, dedicated local profile, and Playwright persistent fallback.
  - Diagnostics and actual browser behavior must share one `BrowserAutomationService`.
  - CDP must stay localhost-only and must not reuse the user's daily Chrome profile.

## 9. OCR URL Extraction and Browser Opening Must Be One Chain

- Symptom: OCR can read text, browser can open pages, but screenshot links are not handed off.
- Root cause: OCR, vision, and browser were separate paths without a shared link-intake step.
- Guardrail:
  - URL intake order is user text, attachment metadata, fast OCR URL extraction, then vision fallback.
  - Recognized URLs are normalized, deduped, evidenced minimally, and handed to browser/CDP.
  - OCR evidence must record provider/latency without persisting full extracted text.

## 10. Session Identity Bugs Need Root-Cause Fixes, Not One-Off Sorting Patches

- Symptom: Conversations cross, renamed chats unexpectedly pin, project/general lists reorder incorrectly, or pasted images lose context on the second turn.
- Root cause: UI-local assumptions could override backend owner/session truth; rename/pin/sort behavior was not bucketed like Codex; attachment context was not fully replayed from history.
- Guardrail:
  - Backend session owner and request owner are authoritative.
  - Rename must not imply pin.
  - Sorting is bucketed: pinned sorted by latest inside pinned, unpinned sorted by latest inside unpinned; project and general buckets are independent.
  - Historical attachments, pasted images, and generated artifacts must be recoverable through replay, refresh, and second-turn contexts.

## 11. Long-Running Tasks Need Projection and Resource Gates

- Symptom: EcoreX gets slower after long sessions or complex tasks.
- Root cause: projection replay, event payload size, image/OCR job observers, scheduler timers, and subagent lifecycle state can accumulate.
- Guardrail:
  - Performance gates must cover RuntimeProjection, frontend render, refresh replay, browser/OCR, image artifacts, scheduler, subagents, cleanup, and idle thread/timer counts.
  - Optimizations must not reduce capability surfaces or hide events needed for audit.
  - Public payloads remain redacted even for `include_events=1` diagnostics.

## 12. User Message Attachments Must Render and Replay Like First-Class Context

- Symptom: File/image attachments in chat bubbles look visually heavy or vanish from later context.
- Root cause: Attachment render and history replay were treated as UI decoration rather than conversational state.
- Guardrail:
  - User text+file+image messages use compact Codex-like attachment rows and light EcoreX-orange bubbles.
  - Built desktop app browser smoke must verify historical attachments, image thumbnails, compact buttons, and second-turn context.
  - Attachment artifacts must be privacy-scanned and avoid raw sensitive paths.

## 13. Privacy Scans Are Part of Development, Not a Final Afterthought

- Symptom: Debug evidence risks persisting raw target hosts, URLs, tokens, file paths, OCR text, or secret-shaped values.
- Root cause: Evidence scripts can accidentally serialize useful but sensitive context.
- Guardrail:
  - Every new artifact path needs a paired privacy scan or a documented reason.
  - Production deploy evidence stores hashes and booleans, not raw host/user/password/URL/output.
  - Runtime events and public API errors use summaries/hashes for exception text and receiver/session identifiers.

## 14. Multi-Agent PASS Means Consensus After Fixes

- Symptom: A slice is marked PASS while one reviewer still has a blocker or the PASS only covers a focused gate.
- Root cause: Review scope and final promotion status can drift.
- Guardrail:
  - Runtime/Backend, Frontend/UX, Harness/Test, Security/Audit, and Release/Regression must all agree before a slice is promoted.
  - Focused-gate PASS is not final PASS unless the final release gate includes it.
  - Review logs must state scope, blockers fixed, and remaining boundaries.

## 15. Final Release Gate Must Fail Closed

- Symptom: A long goal is considered complete while production, package, or privacy evidence is missing.
- Root cause: The final audit only summarized available evidence instead of requiring clean contracts.
- Guardrail:
  - `audit-v023-final-release-gate.py --require-complete` must require all promoted artifact contracts.
  - Adding a new high-risk slice requires adding its evidence to the final audit, not just to prose docs.
  - The final audit must report `complete=true`, `blockerCount=0`, and a current privacy scan before deployment is considered done.

## 16. Login/Authorization Follow-Ups Must Not Drop Browser Tools

- Symptom: The first browser/CDP turn opens a site, the user logs in and replies `已登录`, then the model reads external plugin files or probes CDP through Bash instead of continuing with `browser`.
- Root cause: Short confirmation turns can lose the previous browser/web intent and fall out of the schema budget; raw CDP shell probes are blocked by policy and should not be used as the continuation path.
- Guardrail:
  - Login/authorization confirmations must inherit browser/web intent for one continuation turn.
  - If `browser` is available, raw CDP Bash reroutes must point to the first-party `browser` tool, not to manual port probing.
  - EcoreX must not depend on reading external Codex/Chrome plugin `SKILL.md` files; Chrome DevTools MCP capability is represented through EcoreX config, optional abilities, and browser tooling.

## 17. Deployment Cleanup Is Evidence, Not an Invisible Operator Step

- Symptom: Deployment hangs or fails during public-site installation because the remote temp filesystem is full.
- Root cause: Old release extraction directories, pip cache, and superseded artifacts can fill temporary storage even when root disk still has space.
- Guardrail:
  - A no-space deploy failure must stop the rollout and record a separate cleanup artifact before redeploy.
  - Cleanup evidence must keep hashes/counts/booleans only; raw host, URL, command output, or token-shaped strings must not persist.
  - Privacy scanners stay strict. If an artifact label accidentally looks like a token, rename the evidence label instead of weakening the scanner.

## 18. Smoke Artifact Contracts Must Be Stable

- Symptom: A smoke test really passed, but final gate still blocks because labels/fields drifted from the audit contract.
- Root cause: The generator script and final audit evolved independently.
- Guardrail:
  - When final gate requires a label or field, update the smoke generator and rerun the artifact rather than hand-editing only the audit.
  - Real invocation smokes must prove `prompt -> schema -> tool_call -> execution -> event/projection`, not just a direct import or UI status.
  - Local, packaged, installed, and production artifacts can have separate scopes, but their PASS contracts must be explicit and privacy-scanned.

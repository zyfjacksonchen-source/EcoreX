# EcoreX v0.2.9 WebUI Long Goal

Started: 2026-07-04 21:16:44 +08:00

## Objective

Deliver v0.2.9 for the WebUI-focused EcoreX release. This goal covers the audit surface upgrade, usage-panel effective artifact automation, thumbs-down traceability, knowledge graph frontend display, default identity injection, thinking-motion UI, Tencent Docs MCP WebUI out-of-box capability, version metadata, and focused validation.

## Product Decisions

- Scope is WebUI only. Do not add desktop-only capabilities.
- Audit panel is the management/admin usage panel.
- Fine-grained user actions must be shown in the EcoreX visualization panel.
- Image processing is counted when the user calls `imagegen`.
- Local file processing must not be a top-level action metric because most local-agent work reads or writes local files.
- Unimportant audit metrics should be removed from the visible panel.
- Thumbs-down feedback must show which user marked it and which artifact it belongs to.
- Thumbs-down artifact traceability should use a share-session-style link exposed from `https://mvdcm.ecoremedia.net/ecorex-agent/usage-panel/`.
- Effective artifacts in usage-panel are auto-populated from reported/synced data, not manually filled.
- Effective artifact definition: thumbs up, or no feedback but a final artifact exists.
- Invalid artifact definition: thumbs down or explicitly invalid.
- Knowledge graph frontend display is initially limited to knowledge-base graph data.
- Default identity: EcoreX assistant is called `小芯`, addresses the user as `同学`, and uses a professional, rigorous style.
- Do not proactively ask identity-definition questions during first-run identity setup.
- Thinking motion uses the approved A+D combination: restrained pulse in the main message flow, staged icons in expanded details.
- Tencent Docs MCP is WebUI-only for v0.2.9: connect token, browse/search documents, multi-select documents, and add them to the current conversation/task context.
- Tencent Docs MCP uses the official remote endpoint `https://docs.qq.com/openapi/mcp` and stores the user token only in local workspace MCP config as an `Authorization` header.
- Tencent Docs document selection should behave like a remote attachment flow, not a separate scheduler, project knowledge base, or long-term binding.
- Tencent Docs remote attachments must not be treated as local file paths; backend context should instruct the agent to read content through the discovered Tencent Docs MCP tools.
- Do not run `scripts/真实发布校验.py`.
- Validate online upgrade from v0.2.8 to v0.2.9.

## Slices

1. S00 workspace cleanup and ledger bootstrap.
2. S01 audit taxonomy and admin projection.
3. S02 effective artifacts and thumbs-down traceability.
4. S03 usage-panel Web admin surface.
5. S04 knowledge graph WebUI display.
6. S05 default identity injection.
7. S06 thinking motion upgrade.
8. S07 scheduler module UI readability upgrade.
9. S08 version and release metadata.
10. S09 focused verification and online upgrade smoke.
11. S10 Tencent Docs MCP WebUI out-of-box capability.

## Status Snapshot

- S00 workspace cleanup and ledger bootstrap: completed.
- S01 audit taxonomy and admin projection: completed.
- S02 effective artifacts and thumbs-down traceability: completed.
- S03 usage-panel Web admin surface: completed.
- S04 knowledge graph WebUI display: completed.
- S05 default identity injection: completed.
- S06 thinking motion upgrade: completed.
- S07 scheduler module UI readability upgrade: completed.
- S08 version and release metadata: completed.
- S09 focused verification and online upgrade smoke: completed.
- S10 Tencent Docs MCP WebUI out-of-box capability: completed.

## Current Constraints

- The initial worktree was already dirty before this goal started. Existing user changes must not be reverted.
- Cleanup is limited to temporary generated artifacts and caches.
- Real release validation script is explicitly out of scope.
- Online upgrade validation remains required.

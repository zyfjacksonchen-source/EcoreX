# EcoreX v0.2.4 Native Skill Upgrade Goal

## Objective

Upgrade EcoreX Office/PDF/ImageGen capabilities into EcoreX-native defaults that absorb the best Codex official skill workflows while preserving existing EcoreX entrypoints, runtime invariants, and user-facing compatibility.

This v0.2.4 iteration is WebUI dual-end only: browser/public WebUI plus local WebUI packages for Windows/macOS. Do not plan or implement native desktop installer, DMG, NSIS, signing, notarization, or Electron packaging work except where shared WebUI renderer/runtime source is required.

The v0.2.4 goal includes:

- Office four-piece upgrade: PPT, Excel, Word, and PDF.
- ImageGen upgrade: structural QA, vision QA, reference fidelity QA, auto retry, and final evidence.
- Visual analysis acceleration without reducing QA coverage or defect recall.
- EcoreX-vs-Codex ImageGen efficiency parity benchmark after the image QA/retry chain is complete, using identical prompts and requirements to optimize EcoreX controllable overhead toward Codex-like final usable-image time.
- Skill governance and unified display: group skills first by source (`external`, `custom`, `builtin`) and second by purpose (`system`, `office`, `image/media`, `collaboration`, `data`, `development`, etc.); built-in factory capability packs are default-enabled, always available, and not user-disableable.
- Tongxin Assistant CLI as a default, all-user, read-only capability.
- Feishu/Lark external connection recovery for the case where valid App ID/Secret still reports `lark_oapi not installed`.
- Session list cleanup: remove the general-session robot icon and project-session folder icon, keep unread/completed orange dots and running animation.
- A fresh v0.2.3 reality scan before implementation so the plan cannot drift from the current code.
- Slice-level multi-agent consensus before PASS.

## Non-Negotiable Constraints

- Preserve existing EcoreX skill IDs: `office-presentations`, `office-spreadsheets`, `office-documents`, `office-pdf`, and `image-generation`.
- Keep old API fields and environment variables additive-compatible; new QA evidence must not break old consumers.
- Keep EcoreX ImageGen multi-provider routing; do not replace it with a single Codex `imagegen` path.
- Keep RuntimeProjection and durable runtime events as the source of truth for Web/Desktop state.
- Keep the implementation scoped to WebUI dual-end delivery; shared `desktop/src` renderer edits are allowed because WebUI is built from that source, but native desktop packaging is out of scope.
- Do not optimize by skipping required QA, disabling capabilities, hiding events, or weakening privacy scans.
- Tongxin CLI is read-only only: no write, delete, submit, approve, mutate, or permission-changing operation.
- All skills must use one unified presentation model in WebUI even when their source differs. Source grouping controls governance and toggle policy; purpose grouping controls browsing/filtering.
- Built-in/original factory packs cannot be disabled by users and must be treated as default enabled in API, renderer, release evidence, and package contracts.
- Every slice needs implementation, tests, security/privacy, product/UX, compatibility, and release/regression review agreement.
- Any known debt must be fixed in the slice, explicitly downgraded with evidence, or moved into a named follow-up.

## Slices

- R24-00: v0.2.3 reality scan, baseline, trace, and mapping audit.
- R24-01: Skill registry and EcoreX-native facade for official skill workflow adoption.
- R24-01B: Skill source taxonomy, built-in default policy, and unified capability display.
- R24-02: Tongxin CLI default read-only capability.
- R24-02A: Feishu/Lark external connection `lark_oapi` runtime recovery.
- R24-03: Session list Codex-like visual cleanup.
- R24-04: Unified Office/PDF artifact runtime pack.
- R24-05: PPT story, layout, chart, and visual QA.
- R24-06: Excel model, dashboard, chart, formula, and render QA.
- R24-07: Word presets, comments/redlines, tables, and render QA.
- R24-08: PDF page understanding, extraction, render QA, and visual diff.
- R24-09: Unified Office/PDF Web/API QA evidence.
- R24-10: Image structural QA.
- R24-11: Image vision QA.
- R24-12: Reference fidelity QA.
- R24-13: Image auto retry and finalization policy.
- R24-14: Visual analysis performance optimization with quality-preserving gates.
- R24-14A: CowAgent-style live Markdown rendering parity and streaming smoothness.
- R24-14B: EcoreX-vs-Codex ImageGen efficiency parity benchmark and optimization after R24-10 to R24-13.
- R24-15: Multi-agent consensus release gate.

## Current Baseline Summary

- v0.2.3 source metadata is present in `cli/VERSION`, `desktop/package.json`, release notes, and public manifest.
- Local branch is still named `codex/ecorex-v0.2.0`; local `git tag --list *v0.2.3*` returned no tag.
- Worktree is dirty with many modified/untracked/deleted files from prior release work; do not revert unrelated changes.
- v0.2.3 documentation and artifacts are complete under `docs/v0.2.3/` and should be inherited as guardrails.
- Office built-in skills are present but mostly instruction-level; official Codex artifact skills are visible as `Presentations`, `Spreadsheets`, `documents`, `pdf`, and `imagegen` extra skills.
- Runtime pack currently contains Office/PDF parser libraries and RapidOCR, but no unified render/visual QA artifact runtime.
- ImageGen already has `imagegen` runtime tool, CLI stdin smoke, OCR reuse, parallel image jobs, resource cleanup, and artifact redaction; it lacks post-generation structural/vision/reference QA.
- Session rows currently render either `ThinkingIndicator`, unread dot, project folder icon, or general bot icon; R24-03 removes the non-running/non-unread icons.
- Concurrent v0.2.3 wrap-up thread `019f029a-d132-7b63-9c7b-d1a33b4cef16` has been re-checked after the user reported it ended; it is now idle, so v0.2.4 can proceed while still respecting the dirty-worktree rule.

## Drift Control

- `docs/v0.2.4/development-log.md` records each implementation move.
- `docs/v0.2.4/baseline-scan.md` records current facts and minimal debt/fix candidates.
- `docs/v0.2.4/review-log.md` records per-slice consensus.
- `docs/v0.2.4/acceptance-checklist.md` records pass/fail evidence.
- Before each slice, re-check touched surfaces against v0.2.3 guardrails and update the trace if the code has shifted.
- Re-check any shared surface before editing if another thread resumes; otherwise continue with WebUI dual-end scope and v0.2.4-specific trace/smoke evidence.

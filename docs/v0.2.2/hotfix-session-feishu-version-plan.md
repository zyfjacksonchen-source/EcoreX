# v0.2.2 Web Hotfix Plan

Date: 2026-06-25

## Scope

This hotfix continues the v0.2.2 Web release without changing the core backend-led runtime direction.

- Remove status sweep/light animation.
- Fix new-session contamination where stale history/projection/poll responses can render into a fresh session.
- Stop left sidebar session-list flicker and stale active-session replacement.
- Make the new-session empty state Codex-like: a focused central composer with direct input, project folder entry, and general-chat entry clearly separated.
- Update visible version surfaces, including the top-left version and update/release notes.
- Fix Feishu authorization write-back so a successful auth flow updates persistent/runtime channel state and becomes discoverable/callable by the agent.
- Restore general-session collapse behavior.
- Deduplicate produced artifacts/results across SSE, poll, and RuntimeProjection paths so a request for two images renders exactly two image results, not duplicate pairs.
- Verify locally and through real browser/manual checks before deployment.

## Development Standard

- Code-writing slice owner cannot self-approve the slice.
- Each slice requires multi-agent review from release/regression, frontend/runtime, and security/audit angles.
- A slice only passes after reviewers converge with no remaining P0/P1/P2.
- All deploy evidence must be redacted: no raw host, URL, token, password, open id, chat id, or key.

## Slices

| ID | Slice | Status | Acceptance |
| --- | --- | --- | --- |
| HFX-01 | Session isolation and sidebar stability | AUTO-PASS | Fresh session cannot show old history/projection/poll content; stale async responses are dropped; sidebar list updates do not flicker or overwrite current active state. |
| HFX-02 | Remove sweep animation | AUTO-PASS | No `ecorexTextSweep`, large sheen, or sweep animation remains on status/tool text. Status indicators remain readable and stable. |
| HFX-03 | Codex-like new-session start | AUTO-PASS | New session shows a centered composer first screen with direct input, project folder selection, and general conversation mode. Project and general ownership stay separated. |
| HFX-04 | Version and release notes | AUTO-PASS | Top-left app version and update notes show v0.2.2 from packaged release sources, not stale v0.2.1. |
| HFX-05 | Feishu authorization write-back | AUTO-PASS | Successful Feishu/Lark auth persists the state needed by channel projection and agent discovery; UI no longer reports success while agent calls remain unavailable. |
| HFX-06 | General-session collapse and artifact dedupe | AUTO-PASS | General conversation group collapse remains stable after refresh/new chat; a two-image result renders exactly two artifacts even when SSE, poll, and RuntimeProjection all report the same outputs. |
| HFX-07 | Harness and manual verification | PASS | Browser/API harnesses cover HFX-01 through HFX-11; online Web browser smoke passes against the deployed service. |
| HFX-08 | Review and deployment | PASS | Multi-agent review blockers were fixed, release artifacts rebuilt, target deploy/rollback passed, final deployment succeeded, and online checks passed. |
| HFX-09 | Auth identity projection | AUTO-PASS | Logging in with an explicit account shows that account identity and does not silently fall back to `EcoreX` / `local@...`. |
| HFX-10 | Streaming smoothness | AUTO-PASS | Streaming output uses throttled live text rendering for long in-flight Markdown and browser smoke records chunk-to-paint cadence without visible raw heading markers. |
| HFX-11 | Font baseline | AUTO-PASS | Web UI uses the Codex-like system UI stack; code and monospace surfaces use `ui-monospace` / `SFMono-Regular` / `SF Mono` / Menlo / Consolas stack. |

## Evidence Targets

- `docs/v0.2.2/artifacts/hotfix-contracts-smoke.json`
- `docs/v0.2.2/artifacts/hotfix-react-browser-smoke.json`
- `docs/v0.2.2/artifacts/hotfix-react-browser-smoke.png`
- `docs/v0.2.2/artifacts/hotfix-status-motion-browser-smoke.json`
- `docs/v0.2.2/artifacts/hotfix-markdown-browser-smoke.json`
- `docs/v0.2.2/artifacts/online-web-browser-smoke.json`
- `docs/v0.2.2/artifacts/production-deploy-online.json`

## Current Notes

- Web and desktop-fronted runtime surfaces now share canonical artifact dedupe keys, session reset guards, v0.2.2 visible version fallbacks, Feishu write-back persistence, explicit auth identity projection, removed sweep animation, streaming live preview throttling, and Codex-like font stacks.
- Further deployment steps must preserve backend-owned events/projections and avoid frontend-only truth.
- Current R22-13b gate: Web/Admin deployment is live and release promotion is PASS with an explicit operator waiver for online browser smoke. The failed smoke artifact remains visible; it is not treated as PASS.

## 2026-06-25 Addendum: Auth Identity + Streaming Smoothness

Additional user-reported blockers added to the same hotfix goal:

- HFX-09 Auth identity projection: after logging in with another account, WebUI must show the authenticated account identity rather than falling back to the local virtual user (`EcoreX` / `local@...`). `/auth/check`, `/auth/login`, client session hydration, and any local fallback must be separately observable.
- HFX-10 Streaming smoothness: streaming Markdown/message output must avoid visible stutter. The implementation should reduce full-tree re-render/Markdown reparse pressure, preserve in-progress readability, and add a harness that measures chunk-to-paint cadence instead of relying on manual feel only.
- HFX-11 Font baseline: Web UI typography must use the Codex-like system UI stack (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, etc.) and code/monospace surfaces must use `ui-monospace`, `SFMono-Regular`, `SF Mono`, `Menlo`, `Consolas`, etc. This is a separate visual acceptance slice, not hidden inside the streaming/Markdown slice.

Evidence targets:

- `docs/v0.2.2/artifacts/hotfix-auth-identity-browser-smoke.json`
- `docs/v0.2.2/artifacts/hotfix-streaming-smoothness-smoke.json`
- `docs/v0.2.2/artifacts/hotfix-font-baseline-browser-smoke.json`

## 2026-06-25 Validation Ledger

Automated gates already executed locally:

- Source-contract smoke: `python scripts/smoke-web-hotfix-contracts.py --artifact docs/v0.2.2/artifacts/hotfix-contracts-smoke.json`
- Status/motion browser smoke: `python scripts/smoke-web-status-motion-browser.py > docs/v0.2.2/artifacts/hotfix-status-motion-browser-smoke.json`
- Streaming Markdown browser smoke: `python scripts/smoke-web-markdown-browser.py > docs/v0.2.2/artifacts/hotfix-markdown-browser-smoke.json`
- Full React browser smoke: `python scripts/smoke-web-hotfix-react-browser.py > docs/v0.2.2/artifacts/hotfix-react-browser-smoke.stdout.json`
- Renderer build: `npm --prefix desktop run build:renderer`
- Backend/admin regression subset: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py tests/test_ecorex_admin_device_id.py -q`

Latest observed results:

- Contract smoke: PASS for HFX-01 through HFX-11.
- React browser smoke: PASS; authenticated email visible, v0.2.2 visible, new-session screen visible, Run Center hidden, general collapse stable, duplicate artifacts collapsed from four events to two rendered cards, streaming paint samples average under 50 ms, no raw heading marker visible during live output, UI/code font stacks match target.
- Status/motion smoke: PASS; no sweep animation, no large gradient sheen, only the small status dot pulse remains.
- Markdown smoke: PASS.
- Regression subset: `408 passed, 3 warnings, 11 subtests passed`.
- Historical online browser smoke checkpoint: earlier PASS evidence is superseded by the current R22-13b deployment and the latest `docs/v0.2.2/artifacts/online-web-browser-smoke.json`, which is `FAIL` on final run timing and therefore blocks final release promotion.
- Current R22-13b release gate: `PASS` with `online-web-browser-smoke-valid=waived`; target deploy/rollback and production Web/Admin deployment pass for Web service SHA256 `9631E563D5457B7032228384F139370E49535AA317E9C24BA1A0442F39792D4D` and public release SHA256 `8B4CBA0452BDCBC46D1BC74852AF70DCE53ABF04BED51E2863E37A1D03BB0A77`.
- WebUI desktop-shortcut follow-up: the Windows/macOS WebUI zip artifacts were rebuilt after the Markdown typography hotfix because the earlier v0.2.2 WebUI package still carried stale renderer assets. The promoted Windows WebUI package is now size `83335855`, SHA256 `075FAE724C209A1EB7E9EBF2368775FED872986E82380F64AAC4472545EA1567`, and the macOS WebUI package is size `158450816`, SHA256 `92C9E1D278E6B726CDADED937A35B4C589ECCFCC9C25DC68FB3FCBE34DF21880`.
- Reviewer follow-up fixes: MessageContent artifact shelf now avoids image basename-only dedupe, and Feishu register redaction covers JSON/colon/equal secret and IM identifier shapes.

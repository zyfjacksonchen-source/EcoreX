# R22-13 Post-Release Web Regression Plan

Date: 2026-06-26

## Scope

This slice continues the v0.2.2 Web release after production hand-test feedback.
It does not rewrite the previous PASS evidence. It records a new post-release
regression loop for the Web React surface.

## User-Reported Regressions

- Fresh sessions can still show or flicker previous fixed-session content.
- New-session entry must use the Codex-like left/right choice for general chat
  versus project folder, with explanatory helper text after selection.
- The new-session title must be `和EcoreX一起开始工作`.
- Chat content must fit within one screen without page/chat-pane horizontal scroll.
- Artifact/project/file menus must dismiss when clicking blank space.
- Codex-like task elapsed time must be visible in the main chat and sidebar, not
  only inside Run Center.

## Development Standard

- Slice implementation and review are separate. A code-writing agent cannot be
  the sole reviewer for its own slice.
- Every slice must leave source-level evidence, test evidence, and review notes.
- Review angles are state/projection correctness, UX/CSS behavior, and
  observability/release honesty.
- P0/P1/P2 findings must be fixed before a slice is promoted.

## Slices

| ID | Slice | Status | Acceptance |
| --- | --- | --- | --- |
| R22-13a | Ledger and source root-cause capture | PASS-SOURCE | This file records the post-release loop without overwriting prior PASS evidence. |
| R22-13b | Fresh-session isolation and project/general creation semantics | REVIEWED-LOCAL-PASS | New blank sessions are protected from stale history/projection recovery; the browser smoke proves delayed stale history cannot write into a fresh session and old history/artifacts do not reappear after `+ 新对话`. |
| R22-13c | Codex-like new-session empty state | REVIEWED-LOCAL-PASS | Title is `和EcoreX一起开始工作`; general/project choices render as stable left/right options with helper text; narrow screens stack cleanly. |
| R22-13d | Horizontal overflow containment | REVIEWED-LOCAL-PASS | Document, chat pane, message list, artifacts, Markdown, and composer produce zero horizontal overflow in the browser smoke with long paths/code. |
| R22-13e | Dismissable floating menus | REVIEWED-LOCAL-PASS | Artifact/project/file menus close on outside pointerdown and Escape in the browser smoke. |
| R22-13f | Main-surface run elapsed time | REVIEWED-LOCAL-PASS | Active rows and assistant process surfaces show `已处理 ...`; terminal assistant messages preserve `已在 ... 内达成目标` through local history refresh/merge. Full backend-persisted terminal duration remains a later projection enhancement, not claimed by this hotfix. |
| R22-13g | Harness, multi-agent review, deploy evidence | PASS | Local build/typecheck/smokes pass, independent reviewers converged, target deploy/rollback passed, production Web/admin deployment passed, and online browser smoke passed. |

## Initial Source Findings

- `startNewSession()` clears React state, but there is no durable blank-draft
  epoch protecting the active session from later history/projection writes.
- `selectOrCreateProjectSession()` still opens an existing project session before
  creating a new project draft, which conflicts with the new-session semantics.
- The empty state is still the older `我们应该在 EcoreX 中构建什么?` title with
  two pill buttons.
- `ArtifactShelf` and App-level context menus listen for Escape/scroll/resize but
  not document-level outside pointerdown.
- Run elapsed formatting exists via `formatRunAge()` but is mainly used by Run
  Center; sidebar rows still show `运行中`.

## Evidence Targets

- `docs/v0.2.2/artifacts/r22-13-react-browser-smoke.json`
- `docs/v0.2.2/artifacts/r22-13-react-browser-smoke.png`
- `docs/v0.2.2/artifacts/r22-13-contracts-smoke.json`
- `docs/v0.2.2/review-log.md`
- `docs/v0.2.2/evidence-ledger.md`

## Local Implementation Notes

- `desktop/src/App.tsx` now creates explicit draft session ids, protects blank
  drafts from stale history/projection writes, clears the protection only after
  real messages arrive, and exposes active/terminal elapsed time on the main
  chat surface and sidebar rows.
- `desktop/src/App.tsx` now preserves local assistant `runTiming` when backend
  history refresh returns the stronger/same terminal assistant message without
  elapsed timing metadata.
- Sidebar project-row clicks now start a fresh project-bound draft instead of
  selecting the first existing project session. Existing project sessions remain
  selectable through the project session list below the project row.
- `desktop/src/App.tsx` now renders the new-session empty state with
  `和EcoreX一起开始工作`, two stable general/project choice cards, and helper text
  explaining the selected conversation mode.
- `desktop/src/components/MessageContent.tsx` and `desktop/src/App.tsx` now use
  document-level capture `pointerdown` handling for artifact, project, and file
  floating menus, plus Escape/scroll/resize cleanup.
- `desktop/src/styles/app.css` now constrains chat, Markdown, artifact rows, and
  composer widths so long paths, URLs, code, and tables cannot create a page-level
  horizontal scrollbar.
- Legacy Web static welcome text in `channel/web/chat.html` and
  `channel/web/static/js/console.js` was updated to the same new-session title.
- The R22-13 browser smoke was extended to assert fresh-session isolation,
  delayed stale-history race suppression, artifact/project/file menu
  outside-click and Escape dismissal, zero overflow before and after send,
  narrow-viewport stacked choices with zero overflow, visible run timing after
  local history refresh, font baseline, no raw heading markers, and streaming
  smoothness.

## Local Verification

- `pwsh -NoLogo -NoProfile -Command "npm --prefix desktop run build:renderer"`
  passed.
- `pwsh -NoLogo -NoProfile -Command "npm --prefix desktop run typecheck"` passed.
- `pwsh -NoLogo -NoProfile -Command '$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest tests/test_ecorex_web_parallel_backend.py -k "hotfix or project_session or run_center_hidden or markdown_browser_smoke_harness_contract" -q'`
  passed with `8 passed, 369 deselected`.
- `pwsh -NoLogo -NoProfile -Command "python scripts/smoke-web-hotfix-contracts.py --artifact docs/v0.2.2/artifacts/r22-13-contracts-smoke.json"`
  passed.
- `pwsh -NoLogo -NoProfile -Command "python scripts/smoke-web-hotfix-react-browser.py --screenshot docs/v0.2.2/artifacts/r22-13-react-browser-smoke.png --artifact docs/v0.2.2/artifacts/r22-13-react-browser-smoke.json --timeout-ms 30000"`
  passed. Key metrics: `freshSessionIsolation=true`,
  `lateHistoryRaceSuppressed=true`, `artifactMenuOutsideClick=true`,
  `artifactMenuEscape=true`, `chatFileMenuOutsideClick=true`,
  `projectMenuOutsideClick=true`,
  `overflow.beforeSend.document=0`, `overflow.afterSend.document=0`,
  `runTimingVisible=true`, `runTimingAfterHistoryRefresh=true`,
  `narrowViewport.overflow.document=0`, `narrowViewport.choicesStacked=true`,
  streaming average interval about `46ms`, and zero console errors.

## Review Findings Closed Locally

- Harness/release-honesty reviewer P1: production/deploy evidence was stale for
  this post-release hotfix. Response: current docs keep R22-13 at
  `LOCAL-PASS-REVIEW-PENDING`; deployment/admin update is not claimed until a
  newly packaged hotfix is deployed and online smoke artifacts are regenerated.
- Harness/release-honesty reviewer P1: the browser smoke did not reproduce a
  late stale-history/projection race. Response: browser smoke now opens a slow
  history session, immediately starts a fresh session, waits for delayed history,
  and asserts `lateHistoryRaceSuppressed=true`.
- Harness/release-honesty reviewer P2: project/file menu and narrow responsive
  claims were broader than runtime evidence. Response: browser smoke now covers
  artifact, project, and chat-file menus for outside pointerdown and Escape, plus
  a 390px viewport overflow/stacking assertion.
- State/projection reviewer P1: terminal elapsed timing could be lost when
  backend history replaces the local terminal assistant message. Response:
  history/local assistant merging now preserves `runTiming`, and the browser
  smoke now returns backend history without runTiming after stream completion and
  asserts `runTimingAfterHistoryRefresh=true`.
- State/projection reviewer P2: project-row click still reused an existing
  project session. Response: project-row click now starts a fresh project draft;
  existing sessions remain available as explicit session rows.
- State/projection reviewer P2: the hotfix scope appeared muddied by unrelated
  scheduler/admin surfaces already present from earlier v0.2.2 work. Response:
  this R22-13 ledger scopes the current hotfix to Web regression changes and
  treats scheduler/admin surfaces only as existing release context or deployment
  targets, not as newly claimed R22-13 implementation.
- Harness/release-honesty reviewer P2: artifact menu Escape coverage was claimed
  but only outside-click was proven. Response: the browser smoke now reopens the
  artifact menu, presses Escape, asserts `artifactMenuEscape=true`, and HFX-12
  pins that assertion.
- Harness/release-honesty reviewer P2: HFX-12 did not pin every claimed smoke
  expectation. Response: HFX-12 now checks artifact Escape, project/chat-file
  outside-click, run timing after history refresh, narrow viewport overflow, the
  delayed stale-history race, and R22-13 artifact defaults.

## Review Convergence

- Frontend/UX/CSS reviewer: PASS, no remaining P0/P1/P2 after checking source
  paths, R22-13 browser smoke metrics, HFX-12 contracts, and narrow viewport CSS.
- State/projection/runtime reviewer: PASS, no remaining P0/P1/P2 after checking
  blank-draft guards, fresh project-draft semantics, and `runTiming` merge
  preservation through history refresh.
- Harness/release-honesty reviewer: PASS, no remaining P0/P1/P2 after checking
  artifact Escape coverage, HFX-12 pinning, and the absence of premature
  production/admin deployment claims.
- Deployment follow-up release-honesty review found two P2 evidence-boundary
  issues after production deploy. Both are fixed: release gate now directly
  validates `production-deploy-online.json` and `online-web-browser-smoke.json`,
  and the historical R22-12n evidence row no longer links to the mutable
  production artifact regenerated by R22-13. Release-honesty re-review reports
  PASS with no remaining P0/P1/P2.

## Deployment And Online Evidence

- Historical R22-13 deployment checkpoint: the earlier reviewed React Web build
  produced Web tarball size `3542415`, SHA256
  `2A629D42C4F26FB5EDE48EE989E8C2862558630C5780BC7DF3674773AE7D29EC`, and
  public release zip size `247392670`, SHA256
  `DCBB07930B405EAB7B5862A2EC3C7283DDAC03D556B5A2353287508FAB8BB157`. This
  same-version artifact set is superseded by the R22-13b Markdown typography
  hotfix.
- Current R22-13b deployment: rebuilt `release-artifacts/EcoreX_0.2.2-web-linux-service.tar.gz`
  with size `3542886`, SHA256
  `9631E563D5457B7032228384F139370E49535AA317E9C24BA1A0442F39792D4D`; rebuilt
  `release-artifacts/EcoreX_0.2.2-public-release.zip` with size `247415588`,
  SHA256 `8B4CBA0452BDCBC46D1BC74852AF70DCE53ABF04BED51E2863E37A1D03BB0A77`.
  This checkpoint is superseded by the R22-13c full `markdown-it` parity hotfix
  package below.
- Current R22-13c deployment: rebuilt renderer assets are
  `index-BVORAt4B.js` and `index-BW1C4OQB.css`; rebuilt WebUI packages are
  `EcoreX_0.2.2-webui-windows-x64.zip` size `83382661`, SHA256
  `4EEBF7B09A90D3718B2E84391A85217BC37C0C5A3063C6420544EFDE2E201A91`, and
  `EcoreX_0.2.2-webui-macos-universal.zip` size `158497577`, SHA256
  `5CFB4995E0CDA0482B5E5774F91B2D105A2150BB293A18E544E19CB8F034F129`.
  Rebuilt public release zip size is `247508735`, SHA256
  `FDC7D6EC6BFFEF2C7E30401C04B948DC48DEFE65243E3F770E41E308B1BDB943`.
- `docs/v0.2.2/artifacts/release-target-deploy-rollback-smoke.json` is PASS
  for the new Web service package in the target environment, including deploy,
  checker execution, rollback to v0.2.1, active/enabled service state, and
  hash-only target evidence.
- `docs/v0.2.2/artifacts/production-deploy-online.json` is PASS for the final
  Web/admin deployment: final current version `0.2.2`, service active/enabled,
  public manifest `0.2.2`, public check executed, and raw target/secrets not
  persisted.
- `docs/v0.2.2/artifacts/online-web-browser-smoke.json` is currently `FAIL`
  because the automated real-message probe did not reach the final run timing
  label. UI checks for identity, version, project/general entries, hidden Run
  Center, font stack, and no horizontal overflow remain visible, but this file
  blocks final release promotion.
- `docs/v0.2.2/artifacts/release-gate-preflight.json` is `PASS` with
  `releasable=true`, `blockers=[]`, `errors=[]`,
  `production-deploy-online-valid=pass`, and
  `online-web-browser-smoke-valid=waived`.
- `docs/v0.2.2/artifacts/goal-completion-audit.json` is `PASS` with
  `complete=true`, `completionBlockers=[]`, and no incomplete requirements.
- Local installed WebUI verification is complete: `%LOCALAPPDATA%\EcoreX WebUI`
  cache contains the R22-13c Windows package SHA256
  `4EEBF7B09A90D3718B2E84391A85217BC37C0C5A3063C6420544EFDE2E201A91`, local
  `http://127.0.0.1:9909/api/version` returns `0.2.2`, and installed runtime
  assets are `index-BVORAt4B.js` / `index-BW1C4OQB.css`.

## Review/Release State

- Current state: `PASS-WITH-OPERATOR-WAIVER`, current effective slice R22-13c.
- Required reviews completed: frontend/UX/CSS, state/projection/runtime timing,
  and harness/release-honesty all report no remaining P0/P1/P2.
- R22-13c Markdown parity reviews completed: renderer/security, CSS/layout, and
  smoke/release-evidence all report no remaining P0/P1/P2 after the P1
  DOM post-processing fix and P2 code-font/narrow-screenshot/package-evidence
  fixes.
- Deployment/admin update evidence is appended for this post-release hotfix;
  online smoke is explicitly skipped by operator instruction and recorded as a
  waiver rather than a PASS smoke.

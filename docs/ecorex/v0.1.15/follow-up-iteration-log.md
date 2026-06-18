# EcoreX v0.1.15 Follow-up Iteration Log

## Start State

- Date: 2026-06-18
- Branch: `codex/ecorex-v0.1.15`
- Hand-test marker pushed: `v0.1.15-handtest-pass`
- Hand-test passed commit: `ff0bc81 docs: mark v0.1.15 handtest passed`
- Previous implementation commit: `cf38afb feat: ship v0.1.15 codex-like desktop UX`

## User-Reported Issues

1. Streaming still shows a large raw Markdown chunk, then only becomes formatted after the final output completes.
2. The command/request to view the latest local task log becomes unresponsive after the third conversation.

## Iteration Rules

- Make a rollback checkpoint before code changes.
- Keep implementation and review responsibilities separate across agents.
- Use parallel read-only agents for bug/performance/update/code-quality audits.
- Cross-check fixes until final review agents agree there are no blockers.
- Build to a local hand-testable Windows package.
- Open and test the packaged app locally, record observations, fix, and verify again.
- Include multi-end alignment checks in the hand-test plan. On this Windows workspace, verify Windows desktop and packaged WebUI/static runtime paths directly; record Mac desktop validation as pending when Mac hardware/runner is not available rather than marking it passed.
- Pull performance optimization into a dedicated multi-agent discussion track. Compare the desktop experience against Codex-like mature desktop clients, and separate immediate Win/Mac-safe changes from larger follow-up architecture work.

## Running Notes

- 2026-06-18: Created follow-up iteration log after pushing `codex/ecorex-v0.1.15` and tag `v0.1.15-handtest-pass` to GitHub.
- 2026-06-18: Began fixes for streaming raw Markdown and local log no-response. Streaming plan: split live Markdown at the last complete line instead of requiring a blank paragraph boundary, add table rendering, and accept `content` / `text` / `delta` stream fields. Log plan: make ordinary `/api/logs` requests return a bounded snapshot, keep long-tail behavior only for EventSource, add Electron bridge timeout/allowlist coverage, and force agent text convergence after successful log diagnostics.
- 2026-06-18: Read-only audit findings received:
  - Streaming audit confirmed the blank-paragraph splitter and missing table renderer as likely causes of raw Markdown during streaming; it also requested `content` / `text` / `delta` stream text normalization.
  - Log audit identified `/api/logs` as a 10-minute SSE endpoint, Electron bridge allowlist gaps, lack of sidecar fetch timeout, and generic full-file reads as likely contributors to no-response behavior.
  - Code-quality audit found WebUI `open-path` could launch dangerous workspace files under read permission, relative artifact opening may prefer the wrong workspace path, runtime UI state does not fully persist project/session maps cross-surface, timestamp project IDs are unstable, update artifact arch selection is lossy, and detached SSE replay needs TTL cleanup.
- 2026-06-18: User added that WebUI cannot open project folders or local file links on both Windows and Mac. Added this as a P1 cross-platform WebUI open-path/project-folder fix.
- 2026-06-18: User added that performance optimization should be a dedicated multi-agent discussion track with Win/Mac desktop alignment against Codex-like mature clients.
- 2026-06-18: Implemented WebUI project-folder path registration: browser WebUI prompts for a local folder path, backend validates it, creates `.ecorex/project-memory.md` / `.ecorex/dreams`, registers the folder in the filesystem permission profile, and returns a stable path-derived project id. Existing project-folder open calls also register the path before opening, and relative artifact/file-link opening now prefers the active project-resolved absolute path before the raw relative path.
- 2026-06-18: Added WebUI open-path dangerous extension protection. `open` / `openWith` refuse executable/script extensions; `reveal` remains the safe way to locate those files for manual user action.
- 2026-06-18: Added performance fixes from the dedicated review track: localStorage UI-state writes are now debounced during live streaming, runtime UI-state sync is lower frequency during live streaming and faster after terminal updates, text delta flushes are throttled to about 30fps, and offscreen messages use Chromium `content-visibility`.
- 2026-06-18: Added lightweight Markdown/render memoization for message content and artifact extraction. This is an incremental optimization; the larger normalized transcript-store/virtual-list architecture remains a follow-up candidate.
- 2026-06-18: Fixed package script order so Windows and macOS runtime staging happens after `npm run build`, preventing packaged WebUI/static assets from lagging behind the desktop renderer build.
- 2026-06-18: Verified `npm --prefix desktop run typecheck` and `python -m py_compile channel\web\web_channel.py agent\protocol\agent_stream.py common\ecorex_tool_permissions.py` both pass after the follow-up fixes.
- 2026-06-18: Backend/perf review requested bounded SSE lifecycle. Added replay event caps with absolute event ids, frontend `last_event_id` reconnect cursoring, and guarded TTL cleanup for worker-finalized or detached SSE state with no subscribers.
- 2026-06-18: Backend review requested log path hardening. `/api/logs` and `host_diagnostics` now prefer the active logger `FileHandler.baseFilename` instead of assuming `get_root()/run.log`.
- 2026-06-18: Backend review requested safer WebUI project registration. `/api/project-folder` now registers/validates workspace permission before creating `.ecorex` files and fails closed if the broker rejects or is unavailable.
- 2026-06-18: Packaging/update review found macOS staging lacked WebUI static sync and release tooling defaulted to 0.1.14. Added macOS `desktop/dist -> runtime/channel/web/static/app` sync, package build-before-stage ordering, and release/validator version inference from `desktop/package.json`.
- 2026-06-18: Added release validator assertions for `/api/project-folder`, `/api/open-path`, `data-ecorex-file-path`, `/api/logs`, and `/api/logs/snapshot` so stale WebUI/Electron bridge bundles fail validation.
- 2026-06-19: Found packaged `/api/version` still exposed v0.1.14 release notes during local smoke. Rewrote `common/ecorex_release_notes.py` for v0.1.15, updated the version test and validator expectation, rebuilt the package, and confirmed `/api/version` returns `version=0.1.15` with `releaseNotes.version=0.1.15`.
- 2026-06-19: Rebuilt local hand-test package with `npm --prefix desktop run package:dir`. Output: `desktop/release/win-unpacked/EcoreX.exe`; frontend/static bundle hash: `index-Bii0ViQ8.js` in `desktop/dist`, staged runtime, and packaged runtime.
- 2026-06-19: Ran `python scripts\validate-ecorex-release-artifacts.py --desktop-only --desktop-dir desktop\release\win-unpacked --desktop-node-modules desktop\node_modules`; desktop artifact validation passed.
- 2026-06-19: Opened packaged `desktop\release\win-unpacked\EcoreX.exe` and smoke-tested `http://127.0.0.1:9899/`. Browser title is `EcoreX`, WebUI root loads, and `/api/logs/snapshot` reads `desktop\release\win-unpacked\resources\ecorex-runtime\run.log`.
- 2026-06-19: Smoke-tested `/api/project-folder` with a temporary local folder. It returned a stable project id, created `.ecorex/project-memory.md`, and `/api/open-path` reveal succeeded. Direct `open` of a temporary `.bat` returned the expected dangerous-extension refusal.
- 2026-06-19: Final backend review found two blockers: stale `last_event_id` query could override EventSource `Last-Event-ID`, and macOS `.app` directories could bypass the dangerous extension guard. Fixed both by preferring the header cursor and applying dangerous extension checks to file and directory paths.
- 2026-06-19: Final packaging review found generic `npm run package` could bypass runtime staging and public/update metadata still pointed at v0.1.14. Changed `package` to delegate to `package:win`, rebuilt `desktop\release\EcoreX_0.1.15_x64-setup.exe`, generated `.blockmap` and `latest.yml`, and updated `deploy\ecorex-site\manifest.json` to v0.1.15.
- 2026-06-19: Generated public release bundle with `scripts\prepare-ecorex-public-release.ps1`. Output: `release-artifacts\EcoreX_0.1.15-public-release.zip`, size `151821836`, SHA256 `343426D941C8977D69706B4195B09A837EA975DEF9CD1D1E12D69426E400E575`. Validator passed; only Windows x64 is ready-unsigned, while macOS/WebUI/Linux artifacts are explicitly pending until rebuilt.
- 2026-06-19: Final cross-agent review reached PASS consensus. Frontend review reported no blockers for the requested files. Backend review confirmed SSE cursor precedence, dangerous `.app`/script launch blocking, and update artifact fallback fixes. Packaging review confirmed v0.1.15 manifest/latest.yml/installer/public zip outputs and package staging order.

# EcoreX v0.1.15 Development Log

## Scope

- Branch: `codex/ecorex-v0.1.15`
- Baseline checkpoint before development: `7656474 chore: checkpoint before v0.1.15 codex-like UX`
- Date: 2026-06-18

v0.1.15 continues from the accepted v0.1.14 desktop hand-test state. The user asked to align the desktop chat experience with Codex-style interaction:

- Assistant replies should render as clean text flow, not shadowed cards.
- Streaming Markdown should not flash raw, half-rendered Markdown before becoming structured content.
- Tool/process disclosure should be low-noise and compact.
- Artifact definition and presentation should be explicit, not guessed from final text only.
- File artifacts need selectable local open methods: preview, default local open, reveal in folder, choose app/open-with, copy path.
- Project-folder new-session UX must be explicit, and project sessions must not leak/cross-bind into another project.
- The lower-left Settings and account buttons should not look boxed by default; they show a frame/surface only on hover or keyboard focus.
- The Electron titlebar strip with window controls should use the same color as the left sidebar.
- Text color tokens should use the confirmed Codex-like neutral gray ramp:
  dark `#f4f4f5`, `#a1a1aa`, `#71717a`, `#52525b`; light `#18181b`, `#52525b`, `#71717a`, `#a1a1aa`.

## Design Notes

### Chat Rendering

- Assistant/system messages are rendered as transparent body text; user messages keep the colored bubble.
- Copy affordances are hidden until hover/focus, matching a lower-distraction reading surface.
- Process disclosure is a compact one-line summary with expandable details; tool results stay available but no longer dominate the message.

### Streaming Markdown

- In-progress assistant content is split into a stable Markdown prefix and a small live tail.
- Complete Markdown blocks render structurally while unfinished tails remain plain text or a code block shell.
- Stream deltas are buffered per request and flushed on `requestAnimationFrame`, reducing React update churn under high token frequency.
- Terminal events (`done`, `error`, `cancelled`, media/artifact/tool events) force a flush so no streamed text is lost.

### Artifacts

- Backend SSE now supports structured `artifact` events and request-scoped artifact accumulation.
- Final `done` events include accumulated artifacts, and conversation history extras are updated when possible.
- Frontend artifacts are normalized into `AgentArtifact` and shown in a compact shelf with title, relative/path subtitle, preview thumbnail, and diff stats.
- Artifact actions support preview, default local open, reveal in folder, choose application/open-with, and path copy.

### Project Sessions

- Project sidebar clicks no longer mutate the current session's `activeProjectId`.
- Clicking a project selects an existing project session or creates one if none exists.
- A dedicated project new-session button is visible on each project row.
- Session rows derive project ownership from the canonical `sessionProjects[sessionId]` mapping; cached `sessionUiState.projectId` is normalized from that mapping and never wins during restore.

## Implementation Log

- Added `AgentArtifact` and open-path action types to the desktop API layer.
- Added Electron and WebUI local open support for `open`, `reveal`, and `openWith`.
- Added backend artifact extraction from file events and structured tool results.
- Added artifact persistence hooks and `done.artifacts` payloads.
- Added frontend artifact shelf and action menu.
- Added stable streaming Markdown renderer and rAF delta batching.
- Updated assistant/system message CSS to remove the shadow/card shell.
- Updated process/tool disclosure CSS to compact low-noise rows.
- Fixed project sidebar behavior so project selection enters/creates project sessions instead of rebinding the active session.
- Fixed project add flow so adding/updating a project no longer mutates the current active session before the new project session is created.
- Made `sessionProjects[sessionId]` authoritative over cached `sessionUiState.projectId` when restoring sessions, listing cached rows, and syncing active session UI state.
- Updated lower-left sidebar footer actions to be unframed by default and framed only on hover/focus.
- Updated the Electron titlebar drag strip to use the same `--color-surface` background as the sidebar.
- Updated light/dark text tokens to the confirmed neutral gray ramp and added `--color-subtle` / `--color-disabled`.
- Bumped source/runtime version gates to `0.1.15` while preserving v0.1.14/v0.1.13 compatibility keys.

## Verification Plan

- `npm --prefix desktop run typecheck`
- `python -m py_compile channel/web/web_channel.py`
- `python -m compileall channel desktop/electron -q`
- `npm --prefix desktop run build`
- `npm --prefix desktop run stage:runtime:win`
- Windows directory package via `npm --prefix desktop run package:dir`, followed by a no-rebuild runtime resource sync when needed to avoid package-time hash drift.
- Localhost CSS sample check for assistant no-card rendering, compact process row, artifact menu, and streaming tail.
- Parallel multi-agent audit for UI/UX, artifact protocol, and project-session isolation before final acceptance.

## Verification Results

- `npm --prefix desktop run typecheck`: passed.
- `python -m py_compile channel/web/web_channel.py`: passed.
- `python -m compileall channel desktop/electron -q`: passed.
- `npm --prefix desktop run build`: passed.
  - Renderer assets: `index-Bc1E1o6S.js`, `index-yCt4GcZK.css`.
- `npm --prefix desktop run stage:runtime:win`: passed.
  - Runtime sanitizer reported `PASS sanitized EcoreX release runtime`.
- `npm --prefix desktop run package:dir`: passed; final runtime resources were synced from the staged runtime after package-time rebuild changed renderer hashes.
  - Hand-test executable: `desktop/release/win-unpacked/EcoreX.exe`.
  - Size: `210896896`.
  - SHA256: `CF58A41A44BB0C64E1B80E760EE8CD86735D413F2CABB7BB8E4805DF3E98B154`.
- Final static resource coherence:
  - `desktop/dist/index.html`, `channel/web/static/app/index.html`, `desktop/runtime/ecorex-runtime/channel/web/static/app/index.html`, and `desktop/release/win-unpacked/resources/ecorex-runtime/channel/web/static/app/index.html` all reference `index-Bc1E1o6S.js` and `index-yCt4GcZK.css`.
  - Final packaged runtime sanitizer passed: `python scripts\sanitize-ecorex-release-runtime.py desktop\release\win-unpacked\resources\ecorex-runtime`.
- Packaged runtime version checks:
  - `desktop/release/win-unpacked/resources/ecorex-runtime/pyproject.toml` reports `0.1.15`.
  - `cli/VERSION`, staged runtime `cli/VERSION`, and packaged runtime `cli/VERSION` all report `0.1.15`.
  - Packaged WebUI shim reports `ecorex-web-v0.1.15-web.1` and `EcoreX-WebUI/0.1.15`.
  - Packaged `enterprise-policy.json` uses `ecorex-desktop-v0.1.15` while retaining compatibility keys.
- Browser-rendered localhost CSS sample:
  - Assistant body background transparent, border width `0px`, radius `0px`.
  - Process disclosure background transparent, border width `0px`, height `26px`.
  - Streaming tail transparent and unframed.
  - User message bubble retains themed background/border.
  - Artifact menu renders with stable `136px` width and themed surface background.
- Lower-left footer CSS:
  - Built CSS contains `.sidebar-footer button` default `border-color: transparent` and `background: transparent`.
  - Built CSS contains `.sidebar-footer button:hover` / `:focus-visible` restoring `var(--color-border)` and `var(--color-surface-raised)`.
- Additional UI assertions:
  - Streaming tail renders through `MarkdownBlock` instead of a raw `<p className="streaming-tail">`.
  - Built CSS contains the confirmed neutral gray text tokens.
  - Browser-computed localhost check confirmed `body::before` titlebar background equals `.session-sidebar` background.
- Multi-agent audit status:
  - Artifact/opening protocol audit: PASS.
  - UI/streaming audit initially found raw inline Markdown in streaming tail; fixed by rendering the tail through `MarkdownBlock`.
  - Project/release audit initially found project-add cross-binding risk, cached `projectId` fallback, and package/static hash drift; fixed by removing the early `activeProjectId` mutation from `addProject()`, making `sessionProjects[sessionId]` authoritative, and syncing final source/runtime/package static assets.
  - Final project-session isolation re-audit: PASS. It verified `sessionProjects` is authoritative in `mapSessions`, `restoreCachedSession`, active-session persistence, runtime cache sync, boot `activeProjectId`, and add-project/new-session behavior.
  - Final static/package/token re-audit: PASS. It verified all four WebUI entrypoints reference `index-Bc1E1o6S.js` / `index-yCt4GcZK.css`, channel static assets match `desktop/dist`, neutral gray tokens are present, titlebar/sidebar both use `--color-surface`, and footer buttons are unframed until hover/focus.
  - Final Codex-like chat/artifact re-audit: PASS. It verified no-card assistant/system bodies, low-noise disclosure rows, streaming tail via `MarkdownBlock`, rAF delta buffering, structured artifact flow from SSE/done/history, and preview/open/reveal/open-with/copy actions through renderer/backend/Electron handlers.
  - Consensus: all final re-audit agents reported PASS with no blockers.

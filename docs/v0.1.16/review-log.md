# v0.1.16 Review Log

Each feature records implementation owner, independent review agents, evidence, and current decision.

## Review Roles

- Developer: parent thread implementation. The developer cannot approve the release alone.
- Frontend/UX Performance Reviewer: rendering, focus, responsiveness, and visual behavior.
- Backend/Runtime Reviewer: request lifecycle, SSE, persistence, sidecar APIs, and concurrency.
- QA/Release Reviewer: reproducible tests, smoke coverage, regression risk, packaging, and version alignment.
- Production/SRE Reviewer: observability, crash recovery, resource cleanup, privacy, rollback, and long-run safety.

## Slice 1: Visible Desktop Stability

- Developer: parent thread implementation.
- Scope: F01-F06 first pass.
- Files changed: `desktop/src/App.tsx`, `desktop/src/components/MessageContent.tsx`, `desktop/src/services/ecorexApi.ts`, `desktop/src/styles/app.css`, `desktop/electron/apiBridge.ts`.
- Initial review:
  - Frontend/UX agent `019ee03c-950b-76c2-bad4-5db4b1ecb83b`: PASS on first slice.
  - QA agent `019ee03c-bed4-7e61-97d1-3300cca7d414`: BLOCKED on version alignment, `/api/version` privacy, active session artifact race, nondeterministic skill ordering, and TODO checklist.
  - SRE agent `019ee044-87f4-7e00-8c68-cef6081ce5f4`: BLOCKED on `/api/version` token leak, native stat permission bypass, and incomplete sidecar stop cleanup.
- Fixes applied:
  - Runtime token is no longer exposed by unauthenticated `/api/version`; sidecar verifies the current runtime via a per-boot header.
  - File stat goes through the runtime `/api/file-stat` allowlisted endpoint instead of native Electron stat.
  - Artifact callbacks use message/session project context rather than the active session.
  - Missing/denied artifact rows remain visible with status labels.
  - Long streaming render is throttled and incomplete table tails are held back.

## Slice 2: Backend Runtime and Persistence

- Developer: parent thread implementation.
- Scope: F02, F04, F07, F08.
- Files changed: `agent/protocol/agent.py`, `agent/protocol/agent_stream.py`, `agent/chat/service.py`, `agent/memory/conversation_store.py`, `agent/skills/loader.py`, `agent/skills/manager.py`, `channel/web/web_channel.py`, `desktop/electron/sidecar.ts`.
- Review:
  - Backend/runtime agent `019ee03c-a9f7-79e3-b63b-66572ed25f36`: BLOCKED on switching backend file URLs to `Path.as_uri()` because legacy channels parse `file_url[7:]`.
  - Backend/runtime agent `019ee053-d95b-70c2-97e9-183ee0ebe35f`: BLOCKED on trim persistence when final message length grows back beyond `original_length`.
  - SRE agent `019ee054-b608-7e22-82c9-09ab1b247d28`: BLOCKED on `/api/file-stat` permission-before-existence order and process-tree cleanup gaps.
- Fixes applied:
  - Reverted backend `Path.as_uri()` change and kept file URL normalization in the desktop frontend.
  - Added `new_messages_since_user_query()` and wired agent/chat persistence to the real user-query boundary.
  - `/api/file-stat` now checks the permission broker before filesystem existence checks and does not return resolved paths for denied/missing/error states.
  - SSE replay state uses a lock for queue/event/subscriber/terminal cleanup coordination.
  - TTS late extras attach by assistant `bot_seq`.
  - Windows sidecar stop now invokes `taskkill /T /F` immediately; macOS/Linux sidecar starts in its own process group and receives group SIGTERM/SIGKILL.

## Slice 3: Packaging and Release Gate

- Developer: parent thread implementation.
- Scope: F09 release evidence and v0.1.16 version alignment.
- Files changed: `cli/VERSION`, `pyproject.toml`, `desktop/package.json`, `desktop/package-lock.json`, release scripts, `deploy/ecorex-site/manifest.json`, `deploy/ecorex-admin-api/ecorex_admin_api.py`, `docs/v0.1.16/*`, `docs/ecorex/v0.1.16/installer-repo-README.md`.
- QA/release agent `019ee053-ee5e-7951-8114-1d7b05672109`: BLOCKED on manifest staying at `0.1.15`, public install helper defaults, Admin API client keys, WebUI client key/User-Agent, and TODO docs.
- Fixes applied:
  - Current package/version defaults are `0.1.16`.
  - Admin API default desktop key is `ecorex-desktop-v0.1.16`; WebUI default key is `ecorex-web-v0.1.16-web.1`; old keys remain in compatibility lists.
  - Public install helper defaults to `0.1.16` and merges v0.1.16 desktop/web client keys.
  - Manifest is `0.1.16`; Windows artifact points to the generated v0.1.16 installer; macOS/WebUI/Linux artifacts are marked pending, not old ready artifacts.
  - Acceptance checklist and status docs now contain concrete evidence and remaining blockers.

## Current Evidence

- `npm run typecheck`: PASS.
- `npm run build`: PASS.
- `npm run package:dir`: PASS.
- `npm run package:win`: PASS.
- `python -m py_compile ...`: PASS.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -q`: PASS, 107 passed.
- `python scripts/validate-ecorex-release-artifacts.py --desktop-dir desktop/release/win-unpacked --desktop-only --version 0.1.16`: PASS.
- `python scripts/validate-ecorex-release-artifacts.py --version 0.1.16`: PASS.
- Unpacked desktop smoke: PASS.
- Unsigned installer smoke: PASS.
- Trim-boundary smoke: PASS.
- Public release zip generated: `release-artifacts/EcoreX_0.1.16-public-release.zip`.
- Final public release zip SHA256: `966942A3660F2573155973965FCEB1580D04E25F520164095E9FFFC679BCFD02`.
- Final Windows installer SHA256: `A7984AA3EBA379A8ED4B1553DBD38481DBD82487B0A460E1FC131DCDC0E65D18`.
- Packaged API hand-test: PASS after final package for `/auth/check`, `/api/version`, Windows image POST `/api/file-stat` + `/api/file`, `/api/diagnostics/bundle` path redaction/no image-path leak, and empty `/api/active-requests`.
- Production online verify: BLOCKED because `https://www.ecoreai.cn/ecorex-agent/manifest.json` still serves `0.1.15`.

## Slice 4: Hand-Test Fixes and Promotion Gate Follow-Up

- Developer: parent thread implementation.
- Scope: user-reported hand-test issues plus promotion gate follow-up.
- Files changed: `desktop/src/App.tsx`, `desktop/src/components/MessageContent.tsx`, `desktop/src/services/ecorexApi.ts`, `desktop/src/styles/app.css`, `desktop/electron/apiBridge.ts`, `channel/web/web_channel.py`, release docs, README files, and `deploy/ecorex-site/manifest.json`.
- Fixes applied:
  - Runtime `/api/file` preview paths now use the desktop sidecar preview URL instead of Electron `file://` origin.
  - Image preview uses a large scrollable frame; zoom changes real image width instead of clipping via transform.
  - `@skill` mention results have a max height and vertical scrolling.
  - Done handlers clear assistant `requestId`, suppress locally completed request ids from active UI state, and keep a post-done tail stream for late artifacts/TTS without showing a running state.
  - Backend SSE reconnects after consumed terminal events use a short tail, not a long keepalive.
  - Tool result artifact extraction now recognizes nested `images`/`files`/`outputs`/`artifacts`.
  - Diagnostic bundle API and desktop export button added; bundle emits runtime/request metadata and hash/category log summaries only.
  - Final P2 cleanup: cached-only session rows now ignore locally completed request ids, diagnostic bundles hash local paths/stale locks, and terminal reconnect post-done tails match the 12s backend tail.
- Independent review:
  - Artifact/preview explorer `019ee241-3e1f-7da1-a0d2-eb775fdbab0b`: identified `/api/file` origin bug and nested `images[].url`; fixes implemented.
  - Runtime/SSE explorer `019ee241-88cd-7ee3-8874-334769638075`: identified pending resume placeholder and terminal cursor keepalive risks; fixes implemented.
  - Frontend reviewer `019ee258-46be-7e23-bc08-01e26ded143f`: PASS, no P0/P1 after frontend fixes.
  - Backend/runtime reviewer `019ee258-9195-75f3-b0d9-cc85ccbd5d87`: initially FAIL on diagnostic privacy and short TTS tail; both P1s fixed, backend pytest PASS.
  - Production/release reviewer `019ee258-e8d2-7270-80c1-2149f9659140`: FAIL for public production promotion due to unsigned Windows installer, pending non-Windows artifacts, production download page still on v0.1.15, and remaining UI perf/focus traces. Local artifact hash mismatch was fixed by regenerating manifest/public zip after final package.
- Final re-review after current hand-test fixes:
  - UI/desktop reviewer `019ee27e-1f32-74a2-aa91-f785a850f305`: PASS, no P0/P1; P2 cached-only live row issue fixed in this slice.
  - Backend/privacy reviewer `019ee27e-343a-78f2-a25a-c05b4d2c52ae`: PASS, no P0/P1; P2 path/stale-lock metadata and terminal reconnect tail issues fixed in this slice.
  - Production gate reviewer `019ee27e-491d-7cf2-8bd8-3a9c4015bf4a`: PARTIAL/BLOCK for public production, acceptable as internal Windows hand-test candidate. Current goal explicitly defers signing and prioritizes GitHub push.

## Current Decision

Local v0.1.16 Windows hand-test candidate is ready for manual testing and internal installation. Current scope defers signing and public production promotion; code push is the active delivery gate.

Final independent re-review convergence:

- Frontend/UX agent `019ee053-c27c-7bd1-bbcf-eec175c7ced5`: PASS after table-tail and fenced-code chunk fixes.
- Backend/runtime agent `019ee053-d95b-70c2-97e9-183ee0ebe35f`: PASS after trim-boundary persistence fix.
- QA/release agent `019ee053-ee5e-7951-8114-1d7b05672109`: PASS for prior QA/release P1, with public promotion blockers documented.
- Production/SRE agent `019ee054-b608-7e22-82c9-09ab1b247d28`: PASS for local hand-test candidate, with public promotion blockers documented.

Public production promotion remains BLOCKED until code signing, non-Windows artifacts, production download-page deployment, and automated UI performance/focus traces are completed. This is not blocking the current "先不签名, 先推 GitHub" hand-test scope.

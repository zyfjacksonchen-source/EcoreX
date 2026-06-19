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
- `python scripts/validate-ecorex-release-artifacts.py --desktop-dir desktop/release/win-unpacked --desktop-only --version 0.1.16`: PASS.
- `python scripts/validate-ecorex-release-artifacts.py --version 0.1.16`: PASS.
- Unpacked desktop smoke: PASS.
- Unsigned installer smoke: PASS.
- Trim-boundary smoke: PASS.
- Public release zip generated: `release-artifacts/EcoreX_0.1.16-public-release.zip`.

## Current Decision

Local v0.1.16 Windows hand-test candidate is ready for manual testing.

Final independent re-review convergence:

- Frontend/UX agent `019ee053-c27c-7bd1-bbcf-eec175c7ced5`: PASS after table-tail and fenced-code chunk fixes.
- Backend/runtime agent `019ee053-d95b-70c2-97e9-183ee0ebe35f`: PASS after trim-boundary persistence fix.
- QA/release agent `019ee053-ee5e-7951-8114-1d7b05672109`: PASS for prior QA/release P1, with public promotion blockers documented.
- Production/SRE agent `019ee054-b608-7e22-82c9-09ab1b247d28`: PASS for local hand-test candidate, with public promotion blockers documented.

Public production promotion remains BLOCKED until code signing, non-Windows artifacts, pytest/dependency availability, full diagnostic bundle, and automated UI performance/focus traces are completed.

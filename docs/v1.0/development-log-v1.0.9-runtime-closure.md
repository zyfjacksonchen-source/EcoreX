# EcoreX v1.0.9 Runtime closure log

Date: 2026-07-22 (Asia/Shanghai)

## Root causes closed in this change set

- Input attachments now have an authenticated, account-scoped bounded thumbnail endpoint. Composer and history use the thumbnail; full content is fetched only when the user opens the preview.
- Managed model catalog refresh is executable Runtime state. Bootstrap, Turn admission, Gateway requests and usage projections share one immutable catalog snapshot identity.
- OCR and Office dependency packs are activated through verified Pack-Python subprocess adapters instead of relying on packages accidentally present in Core.
- Capability-pack shutdown on Windows uses native descendant enumeration and termination instead of a silent `taskkill` fallback.
- Test-only local installs receive a temporary Desktop directory and cannot overwrite the user's real EcoreX shortcut.
- Formal platform staging rejects a production Runtime configuration that omits image orchestration or share services and records redacted endpoint evidence in the dependency receipt.
- Installed acceptance is driven through the authenticated WebUI and checks durable event facts for attachments, OCR, tools, image generation, concurrency, retouch, model switching, share, theme and permissions.

## Real v0.2.9.2 history audit

The local released v0.2.9.2 data source was inspected read-only. No title, message text, account credential or conversation identifier was emitted.

- Source inventory: 892 entries, 459,500,173 bytes.
- Canonical non-deleted history: 54 sessions and 1,029 messages.
- UI-cache-only deleted sessions: 93; the migration dry-run excluded all 93.
- Project history: 2 projects and 3 project/thread bindings.
- Current pre-fix v1 state: 7 threads and 18 messages, with zero legacy mappings.
- The released copy-on-write migrator completed a real-data dry-run and produced database-integrity `ok`.

The final late-merge drill used a stable file-family snapshot of the live v1 SQLite database and a temporary install slot. It preserved all 7 existing v1 threads and 18 existing messages, imported exactly 54 legacy sessions and 1,029 legacy messages, and produced 61 threads and 1,047 messages with database integrity `ok`. The report recorded `deleted_session_cache_excluded=93`; no cached-only deleted session entered `legacy_id_map`. The live v0.2.9.2 or v1 directory was never mutated.

The failure was therefore not legacy parsing. The production Bootstrap did not select the legacy source, so Runtime never created a migration plan. Bootstrap discovery and the late-merge path are being made product-owned: a first upgrade imports before Runtime starts; an already-started v1 installation is used as an immutable baseline and merged copy-on-write, then checked as a strict subset before atomic activation. Cached-only deleted sessions remain excluded.

## Verification performed

- Backend focused regression: 251 passed, 14 skipped. One Windows process-tree failure exposed the `taskkill` root cause; the native replacement passes its focused test.
- WebUI contracts: 204 passed.
- TypeScript `--noEmit`: passed.
- Python Ruff checks: passed for all changed Python modules.
- Real v0.2.9.2 migration dry-run: `dry_run_verified` with database integrity `ok`.

## User-facing brand lock

- The user-facing product brand is `e-Mate` in the WebUI document title, login, boot state, left navigation lockup, Composer, settings, share conversation, download page, Admin UI and usage panel.
- The supplied e-Mate lockup and compact mark are used without changing the existing Workbench layout or density. Dark mode applies a display-only colour transform so the black wordmark remains legible while retaining the orange brand accent.
- Internal compatibility identities remain `EcoreX`: API headers, environment variables, local storage keys, package names, filesystem roots, release routes and migration identities are unchanged.
- A frontend contract blocks user-visible workspace copy from regressing to the internal product identifier.

## Release rule

Do not promote stable from source-only evidence. Rebuild the signed candidate, install it into the fixed product root, run the authenticated installed-runtime acceptance matrix, then deploy and read back the public download page, Admin UI, release feed and installer commands.

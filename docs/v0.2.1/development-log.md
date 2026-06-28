# v0.2.1 Development Log

## 2026-06-24 Intake

- User narrowed delivery to Web only. Desktop/Electron packaging and desktop-specific settings are paused.
- User supplied v0.2.1 production target `https://mvdcm.ecoremedia.net/ecorex-agent/`.
- User added screenshots and scenarios for:
  - duplicate final answers;
  - raw Markdown delivery;
  - stale recovery cards staying at the bottom;
  - severe disconnects, especially image generation;
  - artifact menu not closing on outside click;
  - unnamed/timeout-prone subagents;
  - project conversations drifting into general conversations;
  - missing `browser-automation` installer;
  - WorkBuddy-like compact thinking space;
  - Codex-like observability and active-thread visibility;
  - EcoreX-colored shimmer/sheen indeterminate activity indicator.

## 2026-06-24 Historical Read

- Read v0.2.0 `goal.md`, `development-log.md`, `evidence-ledger.md`, `acceptance-checklist.md`, and `review-log.md`.
- Carried forward v0.2.0 lessons:
  - recovery cards must be runtime state, not durable answer history;
  - project/session ownership must merge and preserve explicit mappings;
  - channel/tool/knowledge discovery must share one capability surface;
  - production deploys require both static/download bundle and live Web runtime checks.

## 2026-06-24 Runtime and History Fixes

- `ConversationStore` display grouping now keeps final assistant text in the main answer only, avoiding duplicate conclusion output in the call-process section.
- Final assistant history rows persist turn identity extras: `request_id`, `turn_id`, `user_seq`, and `bot_seq`.
- SSE `done` includes `final_text` and replace semantics so WebUI can replace pending text deterministically.
- SSE `delta` remains append-only; `message_update` is normalized as snapshot/replace.
- Added typed SSE `heartbeat` that the browser uses for liveness but does not render as chat content.
- Added terminal `timeout` support in run ledger state.

## 2026-06-24 Disconnect and Long-Tool Work

- Added adaptive tool lease policy in `agent_stream.py`.
- Tools now emit:
  - `tool_execution_heartbeat`;
  - `tool_execution_deadline_extended`;
  - `tool_execution_timeout`.
- Web SSE maps those to `tool_heartbeat`, `tool_deadline_extended`, and terminal timeout UI.
- Default lease/max/extension are configurable through:
  - `ECOREX_TOOL_EXECUTION_LEASE_SECONDS`;
  - `ECOREX_TOOL_EXECUTION_EXTENSION_SECONDS`;
  - `ECOREX_TOOL_EXECUTION_MAX_SECONDS`.
- Bash max timeout is configurable through `ECOREX_BASH_MAX_TIMEOUT_SECONDS` and defaults to a 2-hour cap.
- Long bash commands without an explicit timeout now receive a 30-minute default, covering image generation, render/export, browser automation install, builds, package installs, and common long test commands.
- This directly addresses the user-supplied image-generation disconnect scenario where outer SSE might survive while the underlying bash command was killed by the old 30-second default.

## 2026-06-24 Web UI State and Rendering

- React stream client keeps EventSource native reconnect behavior and applies a 75-second transient error grace.
- Recovery cards are treated as transient runtime state and are removed/merged by stable turn identity.
- Project/session ownership merge remains explicit so project conversations do not drift into general sessions.
- Markdown final rendering is enabled; live rendering is lightweight for short/medium Markdown and falls back for long streams.
- Artifact menus now close on outside click, Escape, and focus cleanup.
- Added copy surfaces for answer, Markdown, platform/Xiaohongshu copy, artifact files, images, and paths.
- Added compact WorkBuddy-like work/thinking presentation so long tool/subagent logs do not dominate the main answer.
- Added EcoreX shimmer/sheen activity indicator for current phase, active tool state, and reconnect cards. Reduced-motion preferences disable the animation.

## 2026-06-24 Subagent Work

- Subagent API accepts metadata for name/title, summary, expected output, timeout, heartbeat, deadline, and parent request.
- Subagent timeout writes terminal state, emits timeout event, and releases the concurrency slot.
- Web active-request snapshots expose children/`childrenByParent` and subagent metadata.
- Web SSE emits `subagent_start`, `subagent_update`, `subagent_complete`, `subagent_failed`, `subagent_timeout`, and `subagent_cancelled`.

## 2026-06-24 Channels, Knowledge, and Capabilities

- `/api/channels` now returns runtime status fields: `running`, `last_error`, `started_at`, `dependency_missing`, complete `fields[]`, and optional `operation_id`.
- Channel connect/disconnect returns `starting` and `capability_refresh_required`; channel changes reset the bridge/capability cache.
- Web settings/management path restores CowAgent-like channel catalog and distinguishes Feishu/Lark IM bot channel from the `feishu_cli` user connector.
- Existing memory and knowledge APIs remain the Web contract: `/api/memory`, `/api/memory/content`, `/api/knowledge/list`, `/api/knowledge/read`, `/api/knowledge/graph`.
- `browser-automation` is treated as a built-in optional capability when Playwright is available, avoiding the previous `capability installer not found` dead end.

## 2026-06-24 Admin Log and Sync Alignment

- Matched the linked admin/log iteration thread `019ef785-9dac-7922-8654-1b731c2e1af8`.
- Admin API now exposes Web client sync status/policy plus guarded ingest paths:
  - Phase 1: run/tool/artifact event metadata and artifact metadata;
  - Phase 2: chat body storage only when `ECOREX_SYNC_PHASE2_MESSAGES_ENABLED=1`;
  - Phase 3: artifact file chunk storage only when `ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED=1`.
- Phase 1 metadata omits prompt/response/final text bodies, raw paths, raw URLs, and file blobs.
- Phase 2 stores canonical message content with SHA-256 and size accounting behind server policy.
- Phase 3 stores verified base64 chunks with SHA-256 validation, max-size policy, chunk-size policy, per-user leaky-bucket rate limiting, dedupe, and sync summary counters.
- Web bridge producers remain Web-only:
  - EventSource observer sends run/tool/artifact metadata;
  - `/message` acceptance and final `done` can sync visible user/assistant bodies only under Phase 2 policy;
  - same-origin artifact bytes can be chunked and uploaded only under Phase 3 policy.
- mvdcm prior deployment evidence from the linked thread shows Phase 1, Phase 2, and Phase 3 have been deployed and smoke-tested on the new domain without recording credentials in docs.

## 2026-06-24 Image Model Fallback

- Updated `skills/image-generation` so the default call still starts with `gpt-image-2-pro`.
- If `gpt-image-2-pro` is unavailable, model/access blocked, or retryable pro failure is exhausted, the same GPT Image compatible provider retries once with `gpt-image-2`.
- Successful fallback returns `model_fallback` in the JSON payload with source model, target model, provider, reason, and message.
- Invalid prompt/safety/policy failures remain fail-closed and do not trigger model fallback.
- This explicitly prevents silent fallback to Python/Pillow/HTML/SVG generated images while still giving the agent a codex-like autonomous recovery path for pro image-model availability issues.

## 2026-06-24 Deployment Adaptation

- Web runtime/client defaults updated for v0.2.1 and `https://mvdcm.ecoremedia.net/ecorex-agent/`.
- Nginx and Caddy route examples now include `/ecorex-agent/client/*`, `/assets/*`, `/message`, `/upload`, streaming timeouts, and buffering disabled for long streams/uploads.
- Web installer env template includes tool lease and bash timeout configuration.
- Runtime path adapter handles `/ecorex-agent` base paths for fetch/EventSource and file URL generation.

## 2026-06-24 Observability Discussion Round

- Added first Web-visible observability layer:
  - current phase with activity indicator;
  - tool heartbeat elapsed time;
  - deadline extension count;
  - timeout terminal status;
  - Run Center visible by default unless disabled;
  - subagent children snapshots.
- Multi-agent observability consensus:
  - v0.2.1 should not over-animate whole session rows; shimmer belongs on live phase/tool/reconnect indicators.
  - v0.2.2 should add durable run event ledger, global active-session stream, session `runtimeState`/`hydrationState`, capability epoch, lease manager, and agent observation-context injection.
  - Codex-like autonomy needs both architecture and agent-observable facts: lease, heartbeat, stall reason, active sessions, pending permissions, child tasks, and recommended next action.

## 2026-06-24 Final Packaging and Deployment

- Fixed the remaining release-version drift found during deployment:
  - `cli/VERSION` now reports `0.2.1`;
  - `common/ecorex_release_notes.py` now returns v0.2.1 Web release notes;
  - WebUI fallback version is `0.2.1`;
  - Web/public installer defaults are v0.2.1;
  - Web installer output no longer prints the bootstrap password in logs.
- Added `install-ecorex-web.sh` into the public release server-helper bundle so a fresh server can install both public/admin files and the Web runtime from the same release archive.
- Rebuilt the shared Web renderer and all v0.2.1 Web-only release artifacts.
- Deployed to `https://mvdcm.ecoremedia.net/ecorex-agent/`:
  - public download/admin site under `/srv/ecorex-agent-download/current`;
  - admin backend files under `/srv/ecorex-agent-admin/app`;
  - Web runtime under `/opt/ecorex-web/current`;
  - services `nginx`, `ecorex-admin-api`, and `ecorex-web` are active.
- Remote release checker passed against the public domain/path prefix.
- Authenticated Web smoke passed:
  - `/auth/login` succeeds;
  - `/api/version` returns runtime `0.2.1` and release notes `0.2.1`;
  - `/api/channels` returns the v0.2.1 extended channel contract;
  - `/api/active-requests` returns empty active snapshots plus `childrenByParent`;
  - invalid SSE request returns a typed error instead of hanging.

## 2026-06-24 Admin 500 Hotfix

- User reported `https://mvdcm.ecoremedia.net/ecorex-agent/admin/` returning nginx `500 Internal Server Error` after deployment.
- Reproduced the issue only when Basic Auth credentials were supplied:
  - unauthenticated `/admin/` returned expected `401`;
  - authenticated `/admin/` returned `500`.
- Root cause: `/etc/nginx/ecorex-admin.htpasswd` was `root:root 600`, so the nginx worker could not read the Basic Auth file during credential verification.
- Live fix:
  - changed htpasswd ownership/permissions so nginx worker can read it;
  - synchronized Admin Basic Auth credentials across nginx and Admin API;
  - Admin API now supports `ECOREX_ADMIN_USERNAMES=admin,root` so both accepted Basic Auth users can load the page and API.
- Added release-check coverage:
  - htpasswd file must be readable by the nginx worker;
  - optional authenticated `/admin/` and `/admin/api/state` checks run when checker receives admin Basic credentials.
- Rebuilt and redeployed the public/admin release bundle after the hotfix.

## 2026-06-24 Admin Data Migration

- Migrated Admin business data from the original `www.ecoreai.cn` production server to `https://mvdcm.ecoremedia.net/ecorex-agent/`.
- Source DB: `/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3` on the original server.
- Target DB: `/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3` on the mvdcm server.
- Backups retained outside the Web root:
  - original-server export: `/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3.export-mvdcm-20260624183909.bak`;
  - target pre-migration backup: `/srv/ecorex-agent-admin/backups/ecorex-admin.sqlite3.pre-data-migration.20260624183909.bak`;
  - target archived source copy: `/srv/ecorex-agent-admin/backups/ecorex-admin-source-www-ecoreai-cn.20260624183909.sqlite3`;
  - target Admin API file backup: `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.pre-data-migration.20260624183909.bak`.
- Replaced target smoke Admin data with original production Admin data while preserving v0.2.1 sync tables.
- Migrated table counts:
  - `users`: 13 -> 23, with 16 active users;
  - `model_credentials`: 0 -> 1, global `openai/gpt-5.5` enabled;
  - `capability_policy`: 1 -> 1;
  - `client_sessions`: 12 -> 72, with zero orphan sessions;
  - `usage_events`: 4 -> 490;
  - `error_logs`: 1 -> 256;
  - `capability_packs`: 7 -> 7;
  - `audit_events`: 30 -> 1520 after appending a `data.migrate.from_old_server` audit row.
- Added compatibility migration columns for old production data:
  - `error_logs.category`;
  - `error_logs.label`;
  - `capability_packs.preinstall`;
  - `capability_packs.preinstall_reason`.
- Post-migration smoke:
  - `ecorex-admin-api` service active;
  - deployed Admin API compiles;
  - authenticated `/admin/api/state?limit=5` returns version `0.2.1`, active users, and one model credential;
  - `/client/capability-policy` returns `200`;
  - `/client/model-config` with the v0.2.1 Web client key returns expected `401 missing user token` without leaking model credentials.

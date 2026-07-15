# v0.3.0 to v1.0 migration inventory

## Baseline hazards

- The current worktree contains user changes and untracked `agent/core/`; these
  are WIP, not proof of released v0.3.0 behavior.
- Python, Web, README, admin, and release artifacts use different version values.
- Conversation, run/event, and memory data have shared storage paths in legacy
  code; an index recovery path can delete the shared database files.
- Runtime behavior, React source, built static assets, and overlay patches may be
  at different revisions.
- Connector, channel, Skill, MCP, and plugin states do not share one lifecycle.
- No local `v0.3.0` Git tag exists. The GA source commit, GitHub release target,
  release-index commit and later image-hotfix package digest are not one
  consistent provenance chain. An installed workspace therefore cannot be
  attributed to a particular archive from its folder name alone.

## Audited release-data baseline

The canonical migration adapters are derived from commit
`f0750d247bfe52ffb95c137cadc9983a03010690`, the commit recorded as the source
of the first v0.3.0 release package. The relevant data-schema blobs are byte
identical at the last local v0.3.0 image hotfix commit
`9ac3b958a006e82bd53d8a26edf8e119110435d8`:

- `agent/memory/conversation_store.py`: sessions/messages;
- `agent/memory/storage.py`: canonical chunks/files (FTS is rebuildable);
- `agent/protocol/run_ledger.py`: run snapshot;
- `agent/protocol/run_event_ledger.py`: append-only run events;
- `agent/tools/scheduler/task_store.py`: scheduler JSON v1;
- `common/ecorex_tool_permissions.py`: permission JSON;
- `common/ecorex_workspace.py`: WebUI session/project cache.

Every migration report records a schema fingerprint and one of
`release_marker_and_schema`, `release_marker_only`, or
`release_schema_compatible_unattested`. A supplied release marker is hashed and
included in the before/after inventory, but remains `asset_attested: false`
unless the original release archive itself is available and verified.

## Data to import copy-on-write

- User/account binding and non-provider preferences.
- Threads, messages, branches, titles, pins, and project bindings.
- Runs, events, queued work, scheduler metadata, and pending interactions where
  they can be interpreted safely.
- Canonical memories and provenance; rebuildable indexes are regenerated.
- Office deliverables, previews, hashes, path aliases, and source lineage.
- Connector instances, grants, and health metadata.
- Skills/capability packs and their enabled/disabled policy.

## Data not activated in v1

- Legacy provider API keys. They remain only in a local encrypted migration
  backup until the user removes it.
- Legacy transient locks, in-memory approvals, stale stream cursors, generated
  status files, logs, source-code artifacts, and renderer intermediates.
- Electron-only window, titlebar, and permission state.
- Active/queued v0.3 runs and every legacy scheduled task. They are retained as
  recovery drafts or disabled task definitions and require an explicit user
  confirmation under current v1 policy.
- Remembered per-tool grants and filesystem rules. Only the user's
  default/full-access profile intent is staged for the authenticated account.

## Migration safety

1. Identify the actual v0.3.0 release commit or artifact.
2. Inventory and hash source data without mutation.
3. Copy into a new v1 database and content-addressed artifact store.
4. Validate counts, relationships, hashes, and representative previews.
5. Activate only after runtime and Web bundle health checks pass.
6. Keep the v0.3.0 source untouched for rollback before v1 accepts traffic.

The v1 `jobs` table must remain empty for migrated legacy work. This is an
acceptance invariant, not an implementation detail: a v1 Runtime start may not
execute old hidden context, connector actions, tool calls, or schedules.

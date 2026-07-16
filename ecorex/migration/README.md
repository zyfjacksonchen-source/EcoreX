# v0.2.9.2 / v0.3.0 -> v1.0 copy-on-write migration

This package is the standalone migration boundary. It does not mutate or
checkpoint the legacy workspace. A run performs these durable stages:

1. inventory every regular file/symlink and record SHA-256;
2. copy stable DB/WAL bytes into staging, then create SQLite backup snapshots
   from those private copies (SQLite never opens the source files);
3. build a new Runtime database and content-addressed store in a random staging
   directory;
4. import and verify canonical records, relationships, counts, Artifact/CAS
   digests, and sampled Artifact reads;
5. re-inventory the source and atomically rename staging to the target.

Any exception removes staging. An existing completed target with the same
source inventory is validated and returned as an idempotent replay; an unknown
or different target is never overwritten.

## Mapped domains

- `sessions` and `messages` become Runtime Thread/Turn/Item projections plus
  append-only import events. Older schemas may omit documented optional fields.
- `.ecorex/ui-state.json` and session project columns become project records and
  thread bindings.
- memory `chunks` and `files` become canonical records; embeddings/FTS are
  explicitly marked for rebuild, while safe memory files are copied into CAS.
- message `extras.artifacts` are reclassified by `ArtifactService`; source,
  script, diff, log, temporary, path-traversing, and symlinked files are never
  read into CAS.
- connector metadata and Skill enablement are staged without activating old
  credentials or unvalidated code.
- the released `agent_runs` / `agent_run_events` tables are copied as immutable
  historical facts. Runs with a matching conversation Turn keep model and
  terminal state; branch lineage is restored only when the child and parent
  request IDs prove it.
- v0.2.9.2 request IDs reused across multiple conversation Turns are retained
  on every Turn as ambiguous provenance. The one run-ledger row is not falsely
  attached to several Turns, and no message is discarded.
- active/queued v0.3 work becomes a redacted recovery draft with
  `requires_user_confirmation`. It is never inserted into the v1 `jobs` table
  and cannot restart merely because the Runtime starts.
- `scheduler/tasks.json` is validated against the released v1 JSON layout and
  stored disabled pending confirmation. Unknown action contracts remain
  `unsupported_action`.
- the old permission mode is reduced to the v1 `default`/`full_access` intent
  and staged for account binding. Remembered grants and filesystem paths are
  not activated automatically.
- the canonical conversation database remains the deletion authority. WebUI
  cache may enrich an existing session's title, pin, or missing messages, but a
  cache-only session ID is excluded and can never resurrect a deleted
  conversation.
- commit-mode verification must compare the target Thread/session mapping count
  with the authoritative database count and the reported cache-exclusion count.
  A successful dry-run alone is not accepted as proof that deleted sessions
  stayed absent from the published target.
- `config.json` / `mcp.json` secret fields are encrypted with AES-GCM into a
  local quarantine. The key must come from an external credential vault and is
  never stored in the target.

## Entry points

```text
python -m ecorex.migration inventory <legacy-root> --source-version 0.2.9.2
python -m ecorex.migration migrate <legacy-root> <new-v1-root> \
  --source-version 0.2.9.2 --dry-run
python -m ecorex.migration migrate <legacy-root> <new-v1-root> \
  --source-version 0.2.9.2 \
  --quarantine-key-file <vault-exported-key-file> \
  --permission-file <old-user-data>/permissions.json \
  --release-evidence-file <old-runtime>/runtime-manifest.json
```

When the legacy installation `config.json` is outside its Agent workspace, pass
it with `--config-file` (and likewise `--mcp-file`). Explicit external metadata
files are pinned into the same before/after SHA-256 inventory under opaque
labels, so their absolute paths are not written to the report. Conversation and
memory databases remain constrained to the selected legacy workspace.

The local repository does not contain a `v0.3.0` tag, and the historical
GitHub target, release index and later image-hotfix package do not name one
consistent source commit. The migrator therefore reports two separate facts:
released-schema compatibility and optional package metadata. A marker is not
called asset-attested because the original archive bytes are not available to
the migration process. If neither a release marker nor a recognized released
data schema exists, migration fails closed.

Installers call `migrate_legacy_to_v1(..., source_version=...)`; the former
`migrate_v030_to_v1(...)` entry point remains a v0.3-compatible alias. The
target contains `migration-report.json`, `source-inventory.json`,
`backup-manifest.json`, and a secret-free JSONL stage trace. Remaining adapter
work is listed in every report instead of being silently treated as migrated.

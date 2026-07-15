# EcoreX v1 Artifact domain — development record

Implemented 2026-07-10 as the first clean-room v1 office-artifact slice.

## Invariants

- `ArtifactClassifier` is the only visibility authority. UI extension filters are not part of this package.
- Source code, scripts, diffs, logs, temporary files, renditions, source/intermediate output and diagnostics are always `internal`.
- CSV, JSON, HTML, ZIP and Markdown require `explicit_deliverable=True` from a trusted tool declaration.
- Product reads use only `list_user_artifacts` / `get_user_artifact`. Internal IDs cannot be fetched through those methods.
- Artifact/revision/feedback/retouch identities are random opaque IDs. Human filenames use a sanitized minute plus a transactionally claimed sequence.
- Bytes live in an atomic SHA-256 CAS. SQLite stores immutable revisions, current pointers, feedback idempotency keys and retouch lineage in WAL mode.
- Office preview/thumbnail output is an internal rendition nested into its parent projection, never a second user artifact.
- Precise retouch accepts structured normalized annotations. Its annotation layer is an internal artifact and completion creates a new revision that supersedes the requested base revision.

## Integration boundary

The future `/api/v1` adapter should serialize `ArtifactProjection.to_dict()`, never expose `list_internal_artifacts`, and map `ArtifactError.code` to stable protocol errors. `database_path` can point at the Runtime's shared SQLite database; table names are artifact-prefixed.

## Verification

Run:

```powershell
python -m pytest -q tests/v1/test_artifact_*.py
```

Coverage includes coding-file zero leakage, explicit-deliverable gates, nested renditions, 100 same-minute concurrent creations, Windows reserved names, atomic CAS integrity, feedback idempotency, structured retouch/revision lineage and restart persistence.

# EcoreX v1 storage schema authority

## Boundary map

EcoreX has four deliberately separate migration authorities:

| Boundary | Production storage | Authority |
| --- | --- | --- |
| Local WebUI Runtime | one SQLite WAL database plus Artifact CAS | signed local release candidate and `InstallCoordinator` |
| Control Plane / public Share | server database plus object storage | explicit core, audit and share deployment migration jobs |
| Image Orchestrator | PostgreSQL 15+ metadata/leases plus selected S3 or attested single-host encrypted CAS | explicit image-service migration command |
| v0.3.0 import | untouched legacy source plus copy-on-write v1 target | one-time offline importer using the current local schema |

The client candidate migration must never mutate Control Plane, Gateway or
Image-service storage. Conversely, a server migration cannot acquire authority
over a user's loopback Runtime database.

## Local Runtime invariants

- A database with no user tables may be initialized once. Core and every
  feature-domain fragment are created in one `BEGIN IMMEDIATE` transaction.
- Feature flags do not control physical schema. Image, Share, Trace, Connector,
  Tool and Session tables exist even when the related provider is unavailable.
- Every fragment owns a stable ID and exact table/index/trigger inventory. Its
  canonical SQLite definition digest is compiled from source and validated
  before any Repository business write.
- `runtime_meta.product_schema_sha256` binds the whole registered catalog.
  Missing metadata, missing objects, same-name weakened objects or a mismatched
  catalog value fail closed and are never recreated during ordinary startup.
- Domain Repository constructors may perform bounded idempotent business DML
  only after validation. They cannot execute `CREATE`, `ALTER`, `DROP` or set
  journal mode.
- Pre-GA same-version layouts are not guessed from column presence. They require
  an explicitly signed source fingerprint and migration path or remain
  rejected.

The source gate `scripts/check-v1-runtime-schema-authority.py` scans Python AST
string constants and rejects local Runtime DDL outside the core bootstrap,
compiled fragments, signed migration engine and one-time import boundary.

## Compiled local fragments

The current catalog has 17 non-optional fragments:

```text
managed_session, device_authorization, local-memory, connectors-v6,
connector-agent-runtime,
capability-snapshots, runtime-snapshots, tool-executions,
artifacts, integration, extensions, output, runtime-permissions,
sharing, update, system_observability, trace_outbox, audit_outbox
```

Together they cover managed identity, permissions, memory, connectors,
extensions, execution fencing, office Artifacts/retouch, Output, Share,
updates and observability. Binary Artifact/Image content remains in CAS; only
metadata and durable identities are part of SQLite schema authority.

## Signed migration evidence

Candidate storage plans are canonical, bounded and Ed25519-bound. Admission
copy-on-write, live preflight and live apply use the same plan hash and release
identity. Manifest schema v2 also signs the complete target physical schema
SHA-256. Receipts record database digests, row counts, integrity checks and
source/target physical schema fingerprints. Runtime startup revalidates the
compiled product catalog rather than trusting a receipt alone. Manifest v1 has
only a test parser and cannot enter admission, activation or Product startup.

Control Plane core, Cloud Audit and Model Gateway now have independent
version/checksum history, exclusive migration locks, immutable receipts,
complete managed-object fingerprints and explicit migrate/validate commands.
Their Repository constructors use an existing read/write database and only
validate; no replica is allowed to opportunistically create or repair tables.

Cloud Share has the same fixed-target authority plus a separate immutable
media-migration receipt for its one known BLOB layout. The Image service now
uses fixed complete catalogs for both SQLite and PostgreSQL, including the
PostgreSQL constraints, indexes, trigger/function definitions and owned
sequence. A second AST gate permits server DDL in seven exact deployment
migration modules and rejects it in Repository, Store, App and request code.

The v1 migration grammar remains intentionally closed. Adding constraints,
partial indexes, immutable trigger templates, deterministic table rebuilds or
data transforms requires a versioned AST operation; candidate SQL or Python is
never executed. Encryption/hash-chain backfills must be named, compiled
transforms with checkpoint, idempotency and roll-forward receipts.

## Remaining external gates

- Execute a real signed cross-version candidate against an installed corpus,
  including kill points before and after the data barrier.
- Run Windows/macOS archive install/update/rollback using the same catalog
  digest.
- Exercise multi-process SQLite lock contention and long-running transform
  checkpoints.
- Run PostgreSQL/S3 failure recovery and garbage-collection behavior under
  real multi-process load, including the real PostgreSQL catalog/deparser
  integration selected by `ECOREX_TEST_POSTGRES_DSN`.
`connectors-v5` was an unreleased v1 development prototype and is not a user
migration source. Runtime rejects that physical schema without modifying it.
Developers must rebuild disposable prototype databases; the supported v0.3.0
copy-on-write importer creates the final compiled `connectors-v6` layout
directly. Runtime must never run an implicit `ALTER TABLE` repair.

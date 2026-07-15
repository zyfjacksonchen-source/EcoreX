# EcoreX v1 Control Plane production runbook

## Supported deployment boundary

The built-in v1 Control Plane composition is a **single-node SQLite WAL**
service with S3-backed public Share media. It is suitable for one production
process on one encrypted persistent volume. A process/file lock prevents a
second server or an online migration from opening the same database.

This is not a PostgreSQL/HA implementation. Setting the storage backend to
`postgresql` or the replica count above one fails before opening storage. The
code exposes a typed `ControlPlaneProductionProvider` seam for a later,
independently reviewed PostgreSQL implementation; no test/in-memory provider or
SQLite fallback is selected for an HA configuration.

`serve` only validates existing storage. It never creates tables, runs a
migration, creates a bucket, or falls back to local Share object storage.

## Deployment prerequisites

- Python 3.11+, the EcoreX Runtime package and the signed
  `control-plane-cloud` dependency pack (`boto3`).
- One pre-created database directory, one distinct backup directory and one
  bounded Share spool directory. Database and backup volumes must be encrypted
  at rest by the deployment platform.
- One private S3 bucket with default AES-256 or KMS encryption and all four
  Block Public Access controls enabled. SDK credentials come from workload
  identity/the standard boto credential chain; EcoreX has no access-key CLI
  option.
- Short-lived JWTs issued by the managed identity service. Tokens use EdDSA,
  include `kid`, `iss`, `aud`, `iat`, `nbf`, `exp`, `token_use=access`, `sub`,
  `client_id`, `account_id`, optional `organization_id`, and bounded `roles`.
- Trusted Ed25519 public-key rings for identity tokens and signed release
  manifests.
- A trusted HTTPS ingress when binding a non-loopback address. Proxy-derived
  identity/IP headers remain disabled; identity comes only from the verified
  bearer token.

## Environment contract

All settings are environment values. The CLI accepts only an operation; it has
no path, token, key, password or DSN arguments.

Required non-secret settings:

| Variable | Contract |
| --- | --- |
| `ECOREX_CP_STORAGE_BACKEND` | exactly `sqlite-wal` |
| `ECOREX_CP_REPLICA_COUNT` | exactly `1` |
| `ECOREX_CP_DATABASE_PATH` | absolute database file on the persistent volume |
| `ECOREX_CP_BACKUP_DIRECTORY` | absolute, existing, distinct persistent directory |
| `ECOREX_CP_SHARE_SPOOL_DIRECTORY` | absolute, existing private spool directory |
| `ECOREX_CP_STORAGE_VOLUME_ID` | stable identity for the mounted volume |
| `ECOREX_CP_STORAGE_ENCRYPTION_AT_REST` | exactly `true`; deployment attestation |
| `ECOREX_CP_PUBLIC_SHARE_BASE_URL` | credential-free HTTPS URL ending in `/s` |
| `ECOREX_CP_S3_BUCKET` / `ECOREX_CP_S3_PREFIX` | private CAS namespace |
| `ECOREX_CP_S3_REGION` | SDK region |
| `ECOREX_CP_AUTH_ISSUER` / `ECOREX_CP_AUTH_AUDIENCE` | exact JWT trust policy |
| `ECOREX_CP_AUTH_PUBLIC_KEYS_JSON` | `key_id -> canonical base64 raw Ed25519 public key` |
| `ECOREX_CP_RELEASE_PUBLIC_KEYS_JSON` | release-signing public-key ring |
| `ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON` | distinct online pointer-freshness public-key ring; neither key IDs nor SHA-256 fingerprints of raw Ed25519 keys may overlap release keys |
| `ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_PATH` | absolute shared-web-tier path ending in `public-bootstrap-index.json` |
| `ECOREX_CP_PUBLIC_BOOTSTRAP_INDEX_URL` | exact credential-free HTTPS readback URL for that object |
| `ECOREX_CP_PUBLIC_BOOTSTRAP_READBACK_HOSTS` | comma-separated allowlist containing the readback URL host |
| `ECOREX_CP_INSTANCE_ID` | stable signal-consumer identity for this process |

Public Bootstrap freshness renewal is an in-process durable service, not a
daily release build. The immutable release target remains signed by the
offline release key; only its bounded freshness envelope is renewed by a
distinct online KMS/HSM key. Configure the online signer with:

| Variable | Contract |
| --- | --- |
| `ECOREX_CP_BOOTSTRAP_FRESHNESS_AUTOMATION_ENABLED` | `true` by default; disabling is an explicit operator policy and removes scheduler readiness requirements |
| `ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE` | absolute path to the workload-identity KMS/HSM host executable |
| `ECOREX_CP_PUBLICATION_SIGNER_EXECUTABLE_SHA256` | lowercase SHA-256 pin for that executable |
| `ECOREX_CP_PUBLICATION_SIGNER_KEY_ID` | key in `ECOREX_CP_PUBLICATION_PUBLIC_KEYS_JSON`; it must not be a release-authority key |
| `ECOREX_CP_PUBLICATION_SIGNER_ADAPTER` / `ECOREX_CP_PUBLICATION_SIGNER_ADAPTER_SHA256` | optional paired absolute adapter path and digest pin |
| `ECOREX_CP_PUBLICATION_SIGNER_TIMEOUT_SECONDS` | bounded 1–120 seconds; default 30 |
| `ECOREX_CP_BOOTSTRAP_FRESHNESS_LEAD_SECONDS` | renew-ahead window, 1–23 hours; default 8 hours |
| `ECOREX_CP_BOOTSTRAP_FRESHNESS_CHECK_INTERVAL_SECONDS` | scheduler interval, 5 minutes–6 hours and no more than half the lead window; default 1 hour |
| `ECOREX_CP_BOOTSTRAP_FRESHNESS_LEASE_SECONDS` | database lease, 5–30 minutes; default 10 minutes |

The signer receives domain-separated freshness bytes on stdin and returns one
Ed25519 signature; private key material never enters EcoreX. The executable and
optional adapter are re-hashed for every operation, and the returned signature
is verified against the publication keyring before staging. Standard output is
streamed with a 256-byte hard limit and standard error with a 4 KiB discard
limit; timeout or overflow kills and reaps the complete adapter process tree,
and stderr is never exposed. Missing or invalid signer configuration is
explicit `unconfigured` health. When automation is enabled it blocks readiness
even before the first pointer exists, while leaving any database-active pointer
untouched until its signed expiry; it never creates or activates a rollout.
Configuration also proves that lead plus allowed clock skew remains below the
24-hour signed TTL and that one check, lease, signer timeout and skew fit inside
the lead window.

Secret-provider values (the default provider maps these fixed logical secrets
to environment variables; a Vault/KMS sidecar may implement the same narrow
interface):

| Variable | Contract |
| --- | --- |
| `ECOREX_CP_SHARE_KEYRING_JSON` | active/legacy key IDs plus canonical base64 32-byte HMAC keys |
| `ECOREX_CP_AUDIT_ENCRYPTION_KEY_B64` | canonical base64 32-byte AES-256 key |
| `ECOREX_CP_AUDIT_INTEGRITY_KEY_B64` | canonical base64 32–64-byte HMAC key |
| `ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64` | canonical base64 32-byte AES-256 key; required when administrator model management is enabled |

Optional bounded settings include bind host/port, backup interval/retention,
minimum free bytes, audit retention, S3 HTTPS endpoint/addressing style/pool
size, token lifetime/skew, readiness cache, graceful shutdown, concurrency,
backlog and update-signal polling/retention. See
`ControlPlaneProductionConfig.from_environment` for exact names and ranges.
An S3 endpoint, when supplied, must use HTTPS and contain no credentials.

To enable the product administrator workspace, set
`ECOREX_CP_ADMIN_MANAGEMENT_ENABLED=true` and provide the fixed HTTPS preset
map in `ECOREX_CP_MODEL_PROVIDER_ORIGINS_JSON`. The page can then manage users,
usage and tested model revisions without exposing API keys. Gateway and Image
processes must consume the same encrypted management authority as described in
`admin-management-runbook.md`; this is a single-node/co-located contract, not
network-shared SQLite or HA configuration distribution.

## First deployment and schema operations

1. Provision the directories, encrypted volume, bucket controls, workload
   identity and secret/public-key values.
2. Run `ecorex-control-plane schema migrate` as a one-shot deployment job.
   The job takes the exclusive instance lock, creates a verified pre-migration
   backup when data already exists, migrates the core/Audit/Share authorities,
   installs/verifies the persistent-volume marker, verifies WAL health and
   creates a post-migration backup. A partial first migration removes its new
   database; a failed upgrade restores the verified pre-migration copy.
3. Run `ecorex-control-plane schema check`. It performs the complete three
   schema/integrity checks, verifies the newest backup and key configuration,
   verifies release/auth public keys, opens the real repositories, checks S3
   bucket encryption/public-access controls, and performs a private write/head/
   delete probe.
4. Start `ecorex-control-plane serve`. Startup repeats fail-closed repository,
   backup and S3 checks without DDL. The process retains the instance lock until
   shutdown.

Command output contains schema versions, storage mode and a backup receipt; it
does not print paths, endpoints, key material or SDK error text. Failures emit
only a generic exception class.

## Health, draining and backups

- `/health/live` reports only process/lifecycle availability.
- `/health/ready` requires an accepting process, a healthy update-signal
  poller, WAL/receipt health, sufficient database/backup free space, a recent
  verified backup and S3 reachability. When Bootstrap automation is enabled it
  additionally requires a configured signer, a live scheduler task, a fresh
  heartbeat, no scheduler error and a non-expired/non-degraded pointer when one
  exists. A task that exits or misses its bounded heartbeat fails readiness.
  It never returns dependency details.
- SIGINT/SIGTERM marks the service draining before Uvicorn stops. New HTTP work
  receives `503` with `Retry-After`; new update sockets close with `1012`.
  Active sockets are woken during lifespan shutdown, the durable poller stops,
  S3 closes, and only then is the process lock released.
- A verified online SQLite backup runs on the configured interval. Backup
  failure removes readiness and retries after at most 60 seconds. Each copy has
  a ULID, SHA-256, size, reason, volume identity and canonical receipt; retention
  pruning is confined to exact receipt-owned files under the backup root.
- `ecorex-control-plane backup create` and `backup check` provide offline
  operator backup/verification. `backup create` intentionally refuses while
  the service owns the single-node lock; the in-process scheduler is the online
  backup path.

Raw ASGI access logging is disabled because a public Share token is carried in
the request path. Audit records and system metrics must use structured,
redacted observability instead of logging raw URLs or Authorization headers.

## Public Bootstrap freshness operations

Startup performs one catch-up check before the periodic task starts. A due
refresh uses one database-leased, restart-resumable attempt, stores the exact
signed bytes once, reuses the public-object CAS/readback saga, and records
success or failure in audit, outbox and freshness health. A process restart
also reconciles the narrow case where public readback became canonical just
before success bookkeeping was interrupted.
Unexpected scheduler errors are recorded through the same durable failure,
audit and outbox path and retried with a bounded 5–60 second delay; they cannot
silently terminate automation while readiness remains green.

- `ecorex-release bootstrap-freshness-status` prints the durable status,
  current expiry, next check, lease and last safe error code.
- `ecorex-release refresh-bootstrap-freshness` requests one same-authority
  refresh immediately. `--client-request-id` can pin an operator retry; when it
  is omitted the CLI locks a local request journal, binds a new ID to the
  endpoint, purpose and immutable active-authority SHA-256, and fsyncs it before
  the mutating request. Timeout or response loss retains that pending ID even
  when freshness expiry changes; observing a successful response atomically
  completes/clears it so the next intentional refresh gets a new ID. A changed
  authority target invalidates the old pending request and appends a local
  audit event. `--request-journal` overrides the default user-state path. The
  operation cannot change sequence, revision, target or rollout.

Signer, object-store or readback failure retains the previous database-active
pointer, emits a redacted failure event for alerting and reports degraded
readiness. Operators should restore the dependency and use the one-shot command
only when an immediate retry is required; normal recovery is automatic.

## Rollback and incident rules

- Never copy a database while bypassing the SQLite backup API.
- Never delete the volume marker or lock to force a second process online.
- A schema mismatch, stale/corrupt backup, failed S3 control, invalid key ring,
  low disk, audit-chain failure or signal-poller error is a readiness blocker.
- Release rollback does not downgrade a mutated server schema. Restore only a
  verified operator backup while the service is stopped, or roll forward with
  a reviewed migration.
- S3 objects remain private CAS bytes. The service never emits an object URL or
  public ACL; public media is served only through an active Share token.

## Evidence boundary still open

Deterministic tests cover migration rollback, lock exclusion, JWT verification,
S3 policy/write probes, real ASGI lifespan/readiness/drain, no-auto-DDL and
secret-redacted CLI output. This workstation has no real cloud bucket, so a
credentialed S3 outage/latency/permission test is still an environment gate.
A multi-replica Control Plane additionally requires a first-party PostgreSQL
schema/repository provider and real HA/partition/load evidence; the v1 built-in
provider must not be represented as that implementation.

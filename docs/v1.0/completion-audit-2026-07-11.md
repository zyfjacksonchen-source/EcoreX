# EcoreX v1 completion audit — 2026-07-11

This document prevents a green local suite from being mistaken for GA. It was
derived by tracing the user-approved v1 plan into current source and executable
tests. The Goal remains active until every local item is implemented and every
environment item has named evidence.

## Closed during this audit cycle

- Local Runtime schema is one compiled core + 17-fragment catalog; Runtime
  repositories cannot mutate it at startup.
- Control Plane core, Cloud Audit, Cloud Share, Model Gateway and Image SQLite/
  PostgreSQL have explicit version/checksum/fingerprint migration authorities.
- Public Share media moved out of SQLite BLOBs into reference-counted local/S3
  object contracts, with a bounded two-phase migration for the one known BLOB
  source.
- Control Plane audit validation is incremental on hot paths and WSS hints are
  evaluated in bounded 1024-client snapshots.
- New Artifact, revision, feedback and retouch identities use product-wide
  ULIDs rather than UUIDv4.

## Historical local P0 gaps — superseded by the final checkpoint below

| Requirement | Evidence that exposed the gap | Active remediation |
| --- | --- | --- |
| Shell side effects and real sandbox | `shell` was declared idempotent and the process pack carried only a sandbox label | mark non-idempotent, introduce probed SandboxBackend, fail closed when a verified OS sandbox is unavailable, preserve uncertain HITL |
| Chat/image model separation | Turn stored one generic model and image tools accepted an independent model parameter | freeze `agent_model_id` and `image_model_id`; gateway and image handlers consume only their modality |
| Skill/MCP execution | Extension lifecycle existed, but Skill instructions and MCP initialize/list/call did not reach Turn execution | typed contribution snapshot, bounded Skill search/read, isolated MCP supervisor and namespaced ToolSpec bridge |
| General HITL | protocol named information/login/review, while worker/UI implemented mainly approvals/conflicts | typed response forms, durable resolved-input replay and connector/artifact flows |
| Production service composition | Control/Gateway/Image factories required manual object injection | signed/env-vault composition, readiness/drain and API/worker CLIs |
| Signed Capability Packs and Candidate automation | contracts/builders existed without release-pack artifacts or a credentialed `workflow_dispatch` candidate pipeline | build signed browser/image/sandbox packs, platform Runtime archives, scan/sign/publish/promote workflow |

## Historical local P1 gaps — superseded by the final checkpoint below

- Retain one client message/request identity through retry until a durable
  projection acknowledges it.
- Render safe GFM-like office content and progressive tool/checkpoint state
  without exposing internal reasoning or implementation files.
- Bind S3 Share and PostgreSQL/S3 Image adapters into production composition,
  while retaining local stores only for single-node/test modes.

## Environment-only GA gates

- Windows x64 and macOS arm64/x64 WebUI Runtime archive install/update/rollback.
- Real PostgreSQL 15+ and S3/MinIO concurrency, crash, partition and GC soak.
- Real Model/Image providers, Feishu, Tencent Docs and OTLP/RBAC endpoints.
- Domestic mirror, GitHub and CDN outage failover using identical signed bytes.
- Real installed v0.3.0 corpus migration and browser touch/forced-colors/
  reduced-motion/screen-reader/slow-network evidence.

The local host has no running Docker engine, `psql`, MinIO server or MinIO
client; see `evidence/external-ga-environment-local.json`. Mock transports and
SQLite tests are never substitutes for these environment gates.

## Final local source-tree checkpoint

The historical P0/P1 lists above are retained to explain why the refactor was
necessary; they are no longer open source-tree items. The current tree closes
them with backend-authoritative model separation, non-idempotent shell and OS
sandbox contracts, executable Skill/MCP contribution snapshots, typed durable
HITL, production Gateway/Image/Control composition, a durable browser outbox,
real Capability Pack sources and atomic Core+Pack slots.

- Image generation/edit routing is a versioned trusted policy over effects and
  reviewed facets. The Planner contains no `imagegen` or media-route identity,
  keeps read/fetch/vision/CDP/shell discoverable and always honors an eligible
  explicit tool first. Adversarial coverage includes real poster/cover/
  illustration/retouch language, negation, product discussions, failure
  fallbacks, Unicode/size bounds and Skill/MCP name collisions.
- Python 3.11.9 and Node 22.23.1 build inputs are repository locked. Runtime,
  cloud, development and platform-stage Python profiles carry complete hashes;
  CI/Candidate jobs use only the profile installer and `npm ci`. Candidate
  identity and SBOM bind lock manifest
  `c452d89bf9215c89c00638bc7bf39a0eed89a29fd3a63a5917c5abf3d691fa85`.
- The final local suite is `1208 passed, 14 skipped, 0 failed`; every skip is a
  named platform/environment gate. TypeScript is clean, all `138` Web tests
  pass, and the production build emits `17` content-addressed assets under all
  bundle budgets. The post-build gate parses every final JavaScript chunk, so a
  successful Vite run cannot hide a corrupt rehash.
- Real in-app Chromium exercised the final workspace, Artifact preview and
  mobile overflow path. The 1440×900, 1024×768, 768×900 and 390×844 light/dark
  matrix passed 8/8 with zero horizontal overflow and zero axe violations.
  Ordinary controls are transparent at rest, the mobile Artifact action is
  44×44 px and image preview opens fitted with zoom retained.
- The final local Windows Ed25519 candidate drill passed background download,
  explicit activation, bootstrap 200, immutable/no-store caching, a signed bad
  candidate and automatic rollback. It is deliberately recorded as a
  Core-only compatibility drill: this host has no trusted MSVC/macOS toolchain.
  Formal v1 Candidates are required to contain all three Packs and remain an
  external protected-runner gate.

Local source-tree work has no known failing P0/P1 gate at this checkpoint. The
Goal remains active because real platform archives, providers, credentials,
origins, native sandbox probes and installed-user migration data cannot be
manufactured by local mocks.

## 2026-07-12 adversarial re-open

The statement immediately above describes the frozen 2026-07-11 checkpoint,
not the current release decision. Subsequent real-service and independent
release/migration audits reopened local blockers; the Goal remains active while
they are repaired and reverified.

- A real PostgreSQL 16.9 + MinIO drill now covers 256 unique image jobs, 48
  workers, duplicate submission, lease fencing, service pause/restart recovery
  and conditional GC. Production HTTPS/KMS and a multi-hour multi-node soak
  remain external, but shared storage is no longer mock-only evidence.
- Public pointer review found that the repository had a publication client but
  no durable `/api/v1/bootstrap-index` service authority. Stage receipts did
  not prove sequence and stable promotion trusted a locally self-reported
  receipt. Remediation is adding server-owned staged/active/readback state,
  signed freshness, full-authority CAS and same-database promotion proof.
- Migration review found a later-slot lockout caused by treating the original
  migration slot as the forever-current slot, an atomic-publish/receipt crash
  gap, Windows junction traversal and overlap validation that occurred after a
  plan write. Report/schema/quarantine tamper boundaries are also being
  tightened before the migration interface is frozen again.
- Windows Build Tools were subsequently discovered on this host. The native
  AppContainer/Job helper, Bootstrap inclusion and atomic Core+three-Pack path
  are being built and probed locally; no final native or full-Pack candidate
  evidence is claimed until the current security review and drill complete.

These findings intentionally invalidate the old “no known local P0/P1” release
sentence without rewriting history. The current source of truth is
`progress.json`, `ga-gate-matrix.md` and the newest entries in
`verification-ledger.md`.

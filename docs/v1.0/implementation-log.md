# EcoreX v1.0 implementation log

## Recovery header

- Goal: implement the approved EcoreX v1.0 productization architecture.
- Status: active.
- Public target: `1.0.0` directly from the v0.3.0 product line.
- Baseline branch: `codex/ecorex-v0.3.0-hardening`.
- Baseline commit observed at start: `9ac3b958`.
- Worktree policy: preserve all pre-existing modified and untracked files; never
  reset or overwrite them to obtain a clean tree.
- Current recovery point: foundation batch in progress. New v1 modules are being
  built under `ecorex/` with isolated tests under `tests/v1/`.

## 2026-07-10 - Goal initialization

### Completed

- Converted the approved plan into a persistent long-running goal.
- Added the machine-readable recovery pointer and durable engineering ledgers.
- Established `ecorex.__version__` as the v1 product version source and made
  `pyproject.toml` consume it dynamically with a Python 3.11 runtime floor.
- Audited the existing runtime, artifact presentation, UI shape/elevation,
  release scripts, data storage, permissions, and update flow.
- Locked the initial module boundaries: runtime/protocol, office artifacts, and
  update coordinator are developed as separate slices before legacy integration.
- Assigned isolated file ownership to parallel implementation workers to avoid
  collisions in the dirty worktree.

### In progress

- `ecorex/runtime/**` and `ecorex/protocol/**`: event store, Thread/Turn/Item,
  durable jobs, HITL, and `/api/v1` contracts.
- `ecorex/artifacts/**`: authoritative office artifact classification, identity,
  feedback, and structured retouch contracts.
- `ecorex/update/**`: signed release manifest contract and transactional update
  coordinator.
- Root integration: v1 version source, packaging metadata, development records,
  and cross-slice verification.

### Next recovery action

1. Collect the three isolated implementation slices.
2. Review their diffs without touching pre-existing user changes.
3. Integrate package metadata and a single `1.0.0` version source.
4. Run focused v1 tests, record failures, and only then wire `/api/v1` into the
   existing Web runtime behind a feature flag.

### Verification completed in this batch

- `python -m pytest -q tests/v1/test_version_source.py` -> `1 passed`.
- `python -m json.tool docs/v1.0/progress.json` -> valid.
- `python -m compileall -q ecorex` -> pass at the foundation checkpoint.

## 2026-07-10 - Independent foundation audit and capability policy slice

### Completed

- Ran independent read-only reviews of Runtime, Artifact, and Update rather
  than accepting the initial green tests as release evidence.
- Recorded release blockers covering snapshot/watermark gaps, lease fencing,
  API authentication, HITL invariants, artifact classification bypasses,
  internal-ID leakage, cross-process CAS races, user-confirmation bypass,
  fail-open signature verdicts, and staging TOCTOU.
- Converted each review into an isolated remediation batch with explicit file
  ownership and a "failing regression first" requirement. These batches are
  still in progress and the affected domains are not considered verified.
- Added `ecorex/capabilities/**`: a backend-owned versioned `ToolSpec`
  registry, availability and governance stages, direct/deferred/hidden
  exposure, deterministic routing snapshots, progressive `tool_search` /
  `tool_describe` / guarded `tool_call`, and redacted invocation records.
- Added a managed model catalog with modality-specific defaults and strict
  canonical alias resolution. `image2`, `image-2`, `IMAGE_2`, and
  `gpt-image-2` resolve to the same image model without requiring a Thread.
- Verified that image intent promotes image generation without deleting
  `read`, `fetch`, `vision`, CDP, or shell candidates. Unknown tools and models
  fail closed; full access skips approvals but never overrides administrator
  hard-deny.

### Verification

- `python -m pytest -q tests/v1/test_capability_planner.py
  tests/v1/test_capability_invocation.py tests/v1/test_managed_model_catalog.py`
  -> `12 passed`.

### In progress

- Runtime audit remediation: consistent SQLite read snapshots, lease fencing,
  authenticated local API/CSRF, HITL and terminal-state invariants, event
  immutability, idempotency and durable SSE semantics.
- Artifact audit remediation: normalized fail-closed classification, trusted
  deliverable declarations, safe revision/lineage DTOs, cross-process CAS and
  atomic retouch/rendition transactions.
- Update audit remediation: explicit activation confirmation, strict verifier
  verdicts, recovery re-verification, platform/version admission, safe slot
  switching, bounded downloads and known-good semantics.

### Next recovery action

1. Collect and review the three remediation batches.
2. Run each domain suite independently, then the full `tests/v1` suite.
3. Mount the managed catalog and capability snapshots into authenticated
   `/api/v1/bootstrap` and Turn creation only after the Runtime security patch
   has landed.

## Batch completion template

Append one section per batch containing:

- Scope and owned files.
- Decisions added or changed, with ADR references.
- Tests and exact results.
- Known gaps and rollback path.
- The single next recovery action.

## 2026-07-10 - Hardened Runtime, Artifact authority, migration and thin WebUI

### Completed

- Hardened the SQLite event store, Thread/Turn/Item state machine, durable job
  leasing/fencing, HITL recovery, idempotency, authenticated SSE, exact
  loopback Origin/CSRF checks, and the 72-hour durable security lease.
- Added immutable Runtime configuration/model/permission snapshots and durable
  capability snapshots. Turn admission now canonicalizes managed models and
  captures config, capability, permission, and model-catalog IDs atomically;
  Job lifecycle events recover the same context from an immutable relation.
- Replaced hard-coded bootstrap model/connector lists with backend-owned model,
  capability, and connector catalogs. Image intent promotes `imagegen` without
  removing read/fetch/vision/CDP/shell.
- Added a same-origin product server that verifies a signed release manifest
  and signed Web bundle manifest before serving an in-memory, allowlisted React
  bundle. It enforces loopback/Host boundaries, no-store HTML, immutable hashed
  assets, CSP, and per-process bearer injection.
- Completed office Artifact classification, content-addressed storage,
  revisions/lineage, feedback and structured retouch. Added immutable
  account/thread/turn ownership; the public API cannot enumerate another
  account or internal implementation artifacts.
- Mounted Artifact routes under Runtime security. Public Artifact events first
  commit to a leased SQLite outbox and then idempotently append to the owning
  Thread event stream with the Turn snapshot context.
- Completed copy-on-write v0.3 migration for conversations/messages/projects,
  memory, declared office artifacts, connector metadata and skill state. Legacy
  secrets are AES-GCM quarantined with an externally supplied key and never
  activated or uploaded.
- Hardened InstallCoordinator recovery, signature/digest revalidation, exact
  first-install pins, user-confirmed activation, safe slots, known-good
  rollback, size/disk bounds and cross-platform archive extraction.
- Added the deterministic release builder for Windows x64 and macOS arm64/x64,
  real Ed25519 artifact/manifest signatures, fixed source priority, atomic
  output and CycloneDX inventory. The independently signed Web manifest is the
  current follow-up batch.
- Promoted `desktop/src/v1/AppV1.tsx` to the only renderer entry. Removed the
  legacy React/App CSS source and tracked Electron build/runtime/signing chain.
  The remaining WebUI package has no Electron, tldraw, markdown-it, wait-on or
  electron-builder dependency and reports zero npm vulnerabilities.
- Implemented the single clipped WorkspaceSurface, semantic OKLCH tokens,
  hover/focus/touch Artifact action rail, authenticated Artifact list/preview/
  download/feedback, inline image previews, and a structured rectangle-based
  precise-retouch workspace without CSP-incompatible inline styles.

### Independent verification highlights

- Runtime reviewer: 33 Runtime tests passed; product server 17 passed and one
  platform-permission skip.
- Artifact domain after ownership integration: 88 tests passed.
- Migration reviewer: 12 migration tests passed; the last stable full v1 tree
  before the concurrent Web-manifest follow-up was 242 passed, 4 skipped.
- Update reviewer: 42 update tests passed, 2 platform skips.
- Release builder reviewer: 22 builder tests passed, 1 platform skip.
- WebUI: 11 reducer/transport tests passed; TypeScript passed; Vite produced one
  376.40 kB hashed JS asset and one 29.79 kB hashed CSS asset; npm audit is zero.
- Design gate now requires the legacy monolith to be absent and reports zero
  legacy colour/radius/shadow/z-index debt.

### Known open work (not complete)

- Model Gateway, actual Agent job worker/tool handlers, checkpointed long-task
  execution and cloud Control Plane/WSS push are not yet wired.
- Connector persistence/OS credential vault is in independent final review.
- Release WebBundleManifest builder, updater HTTP transport, trusted-key
  rotation/revocation, admin rollout service and install bootstrap are active
  follow-up batches.
- Migration canonical project/memory/connector/skill consumers still need
  Runtime APIs; older unknown v0.3 schema variants remain explicitly reported.
- Browser screenshot/accessibility regression and Windows/macOS real-machine
  update/permission tests remain release blockers.

### Next recovery action

1. Finish and merge connector durability and signed Web-manifest builder.
2. Implement the Agent worker/Model Gateway boundary and durable audit/replay.
3. Add update/control-plane APIs and wire the WebUI activation flow.
4. Run the full v1 suite from a quiescent tree, then browser visual/a11y matrix.

## 2026-07-10 - Managed Model Gateway worker lifecycle

### Completed

- Added a strict managed Model Gateway transport boundary and a lease-fenced
  `AgentTurnWorker`. Model deltas, tool calls, continuation requests, retries,
  terminal state, and HITL checkpoints now persist through the Runtime rather
  than through browser state.
- Added durable tool-execution identity and result records. Non-idempotent
  executions with an uncertain prior result stop at a conflict-resolution
  interaction instead of being silently repeated.
- Added `AgentWorkerSupervisor` as the ASGI lifecycle owner for a bounded worker
  pool. It starts only when a managed gateway is explicitly injected, drains on
  shutdown, cancels after a bounded timeout, and relies on expired durable
  leases for crash recovery.
- Separated the managed model catalog from model-service availability in
  `/api/v1/bootstrap`. The catalog is still available before the first message;
  an unconfigured gateway is represented as `unavailable` instead of a false
  ready state. The thin WebUI preserves model selection and disables only model
  submission while history and local Artifacts remain usable.
- Product server settings can inject the gateway and capability handlers into
  the local Runtime without placing provider credentials in the React bundle.

### Verification

- `15 passed, 1 skipped`: supervisor lifecycle, Worker, Runtime API, and signed
  product-server tests.
- `27 passed`: Runtime composition, hardening, and version-source regressions.
- `npm run typecheck` passed and `npm run test:v1` passed all 11 reducer/client
  tests after adding the `model_service` contract.

### Known open work (not complete)

- The Control Plane must still provision/refresh the gateway credential and
  publish dynamic service health; the current injection point is intentionally
  credential-provider agnostic.
- Office tool handlers still need to create scoped Artifact revisions and
  conversational Artifact Items through the authoritative services.
- Connector durability and the real SHA-named Web production build are still
  running independent final checks.

### Next recovery action

1. Finish real SHA-256 Web dist generation and its signed release/server E2E.
2. Integrate durable connector instances, vault-backed auth, health and outbox
   into Runtime bootstrap and `/api/v1` routes.
3. Implement persistent permission settings and the update/control-plane API.

## 2026-07-10 - Persistent permission authority and content-addressed Web dist

### Completed

- Added a SQLite-backed permission authority for the local account. Default and
  full-access preferences survive Runtime restarts, mutations require CSRF and
  a client idempotency ID, and an append-only request audit records each change.
- Every future Turn now obtains a newly verified immutable permission snapshot
  from the authority. Existing Turn/Job contexts retain their original policy;
  delayed retries cannot resurrect a permission profile the user subsequently
  revoked. Administrator hard-denies remain effective under full access.
- Added the settings mutation contract and thin WebUI control. The current mode
  is continuously visible, enabling full access is explicit, and returning to
  default is one click; errors remain in the Runtime transport error channel.
- Replaced Rollup's non-authoritative filename hash with a deterministic
  post-build content-addressing gate. It rewrites the asset dependency DAG and
  `index.html`, verifies every final filename against final-byte SHA-256, then
  swaps the directory atomically. A second run is byte-identical.
- The Web gate rejects missing/orphaned/cyclic assets, links/reparse points,
  unsafe paths/types, inline script/style, external entry resources and all
  known legacy overlay markers. Real Vite output now passes through
  `WebBundleBuildInput`, signed release manifests and the product-server loader
  in one executable E2E.

### Verification

- Permission API/composition/supervisor/capability focused checks: 9 passed;
  dedicated permission persistence/hard-deny/delayed-retry checks: 2 passed.
- Web: TypeScript passed; `npm run test:v1` passed 15 tests; design gate passed;
  npm audit reported zero vulnerabilities; real production build passed.
- Signed real-dist release/server E2E and Web/release domain: 20 passed, one
  platform skip (extended release/server run: 37 passed, two skips).

### Known open work (not complete)

- The permission batch is under an independent adversarial review; findings
  must be merged before its milestone is treated as release-ready.
- Cyclic Rollup chunk references are deliberately a release error because
  recursive content addresses have no stable byte identity. Future code
  splitting must keep the emitted asset dependency graph acyclic.
- Runtime update API/control-plane push, update activation UI, and cross-platform
  package activation tests are still open.

### Next recovery action

1. Merge permission and connector independent audits.
2. Mount connector state/auth/health/action APIs and dynamic bootstrap catalog.
3. Add signed update-feed transport, background preparation and explicit
   “update and refresh” activation contract.

## 2026-07-10 - Durable Connector mount and signed Bootstrap supervisor

### Completed

- Added a versioned Connector repository with durable OAuth/instance/health/
  invocation/idempotency/outbox state, lease fencing, restart recovery and
  fail-closed OS credential-vault selection. OAuth state is stored only as a
  digest; the exact loopback callback is protected by one-time state, PKCE,
  expiry and consumption fencing.
- Mounted the backend-owned Connector catalog, OAuth begin/callback/complete,
  health, invoke, disconnect and uncertain-operation resolution contracts at
  `/api/v1/connectors`. Bootstrap now projects adapter availability and current
  health, and newly accepted Turns record a fresh availability/configuration
  snapshot rather than trusting React state.
- Bridged Connector outbox facts idempotently into an internal Runtime audit
  Thread. Public connector output is validated against action-specific schemas
  and rejects credential material, sensitive URI components and undeclared
  fields.
- Added the signed slot Bootstrap supervisor. It validates slot metadata,
  receipt, payload identity and known-good pointers independently, launches the
  Runtime without a shell using a constrained environment, handles the
  dedicated restart exit code, forwards signals and bounds restart attempts.

### Verification

- `58 passed, 1 skipped`: Connector contract/persistence/vault/Runtime mount and
  Bootstrap supervisor suites. The skip is a platform-specific credential-
  manager check on the current Windows environment.

### Known open work (not complete)

- The React Connector popover still needs live connect/reconnect/disconnect
  actions and OAuth completion polling; that work is active in a separate
  frontend batch.
- Feishu and Tencent Docs production adapters still require real credentials,
  provider sandbox tests and signed capability-pack delivery before GA.
- The online updater factory must wire its restart requester to the Bootstrap
  supervisor; the update/control-plane chain is in independent adversarial
  review.

### Next recovery action

1. Finish live Connector WebUI actions without exposing credentials.
2. Finish update/control-plane WSS and signed activation review.
3. Merge Replay/trace/audit outbox, then run the quiescent full v1 suite.

## 2026-07-10 - Durable cloud Managed Model Gateway boundary

### Completed

- Added the cloud-side authenticated managed-model stream service. Account
  principals carry an explicit model allowlist and quota period; local Runtime
  requests cannot select an undeclared provider or submit provider keys.
- Added a WAL-backed request/event ledger. A request ID is bound to its full
  request digest and account, each provider event commits before NDJSON is
  emitted, duplicate delivery replays the same terminal stream, and terminal
  event plus request completion commit atomically.
- Added lease fencing and fail-closed crash recovery. An expired active model
  request is not invoked a second time; it is fenced and converges to a
  retryable `gateway_execution_uncertain` fact. Append-only triggers and event
  digests detect mutation.
- Changed local Agent model-request identity to include the durable Job attempt.
  In-attempt replay keeps one identity, while an explicitly scheduled retry can
  make a new provider attempt instead of replaying the previous retryable
  terminal failure forever.

### Verification

- `16 passed`: managed Gateway client/server and Agent worker focused suites,
  including auth, allowlist, quota, idempotent replay, provider-error redaction,
  sequence validation, expired-lease fencing, tamper detection and new-attempt
  request identity.

### Known open work (not complete)

- The server requires an independent security review before release-ready
  status. Production still needs the account-session authenticator, quota
  policy source, real cloud provider Adapter and deployment/TLS controls.
- A real long-stream/disconnect soak is still required; focused tests exercise
  cancellation and recovery contracts but not an external provider network.

## 2026-07-10 - Signed online update and rollout Control Plane closure

### Completed

- Corrected the WSS client/server contract so channel, platform, architecture
  and current version participate in the real handshake. WSS requires hostname-
  validating TLS and forbids redirects, preventing a bearer token from being
  forwarded to another origin.
- Hardened release feed and resumable artifact transport around content
  encoding/length, exact ranges, parent links/reparse points, hardlinks,
  exclusive file creation and opened-file identity checks.
- Activation now re-fetches the authoritative feed and requires the staged
  manifest to match in full, then re-verifies manifest/artifact signatures.
  Pause or kill switch can durably cancel before pointer switch; recovery from
  drain/activate reauthorizes, while a switched pointer follows an explicit
  roll-forward health/known-good path.
- Made channel kill switch durable and added an explicit clear operation. New
  rollouts and client feeds fail closed while killed. Control Plane audit-chain
  verification runs at transactional and distribution reads.
- Added `build_product_update_composition`, which wires trusted Ed25519 keys,
  HTTPS feed, WSS hint, hardened fetcher, InstallCoordinator,
  RuntimeUpdateService and the delayed Bootstrap restart requester without
  permissive health/drain/migration defaults.
- Update shutdown now waits for in-flight blocking work and idempotently closes
  feed, signal source and coordinator fetcher. WSS wake ordering no longer loses
  an update hint.

### Verification

- Independent batch full v1 report: `361 passed, 7 skipped`.
- Root reproduction: `20 passed, 1 skipped` for update transport/service, real
  TLS WSS and Control Plane rollout/feed contracts.
- The real network test launches a TLS Uvicorn listener with a trusted test CA,
  activates a rollout and receives `update.available` through `websockets`.

### Known open work (not complete)

- The in-memory WSS hub is single-process; production multi-instance Control
  Plane needs Redis/NATS-equivalent shared Pub/Sub. Five-minute signed feed
  polling remains the correctness fallback.
- A narrow distributed TOCTOU remains between feed reauthorization and the
  local pointer switch. Eliminating it entirely requires a short-lived signed
  activation lease bound to release, client and staged digest.
- Public CA/load balancer, KMS key rotation, live domestic mirror/GitHub/CDN
  outage drill and signed platform packages remain deployment/GA gates.

## 2026-07-10 - Live Connector WebUI lifecycle

### Completed

- Added strict Connector catalog/auth/health/disconnect TypeScript contracts and
  Runtime client methods with authenticated mutation headers and stable client
  request IDs.
- Extracted Connector lifecycle state from the main Runtime session hook into a
  dedicated module. It owns catalog refresh, HTTPS authorization URL checks,
  45-second bounded OAuth completion polling, connect/reconnect/health/
  disconnect state and recoverable errors.
- Replaced the display-only popover with stable/Beta grouping, explicit
  unconfigured/connected/degraded/error states, add-account/reconnect/check and
  second-confirmation disconnect actions. Keyboard, touch targets, loading,
  disabled, notice and error states use the locked Design System tokens.
- Fixed production API base normalization, which previously could turn the
  injected `/api/v1` base into `/api/v1/api/v1` and make every Connector action
  fail only in the signed bundle.

### Verification

- Root reproduction: TypeScript passed; `npm run test:v1` passed 19 tests;
  Design System gate reported zero raw colour/radius/shadow/z-index/
  `transition: all`/layout-transition violations; production Vite build and
  final-byte content-addressing completed with two assets.

### Known open work (not complete)

- Connector mutations must still persistently consume the submitted
  `client_request_id`; a backend follow-up is active.
- OAuth callback needs a safe close/parent-notification page, reauthorization
  needs one atomic backend operation, and non-OAuth credentials must remain
  disabled until an OS-vault-only submission contract exists.
- Browser screenshot, focus, touch and responsive verification remains a final
  UI gate; static/build checks do not replace it.

## 2026-07-10 - Durable thread catalog and authoritative history projection

### Completed

- Added authenticated `/api/v1/threads` keyset pagination with active/archived
  filters. The opaque cursor is HMAC-bound to its filter and now enforces one
  canonical URL-safe Base64 representation, so textually altered cursor aliases
  fail closed even when they would otherwise decode to the same bytes.
- Added idempotent rename, archive and restore mutations. Replayed stale client
  request IDs return the current projection and cannot roll a later title or
  lifecycle state backward.
- Added first-Turn server-side title generation and made every committed Thread
  event advance `threads.updated_at`, keeping catalog ordering and deterministic
  Replay on the same event-defined timeline.
- Extended Mock Replay for rename, generated title, archive and restore events;
  the replayed Thread projection is checked against the authoritative Runtime
  projection rather than UI-local state.

### Verification

- `14 passed`: thread catalog, Replay/observability and Runtime kernel/API
  focused suites, including cursor tamper/filter rejection, pagination without
  duplicates, stale mutation replay, CSRF and whitespace-title rejection.

### Known open work (not complete)

- React task navigation, rename/archive controls and ShareSnapshot UI still
  need to consume these backend contracts.
- Final multi-process/full-suite verification is intentionally deferred until
  the concurrently hardened Connector, Gateway and retouch branches are idle.

## 2026-07-10 - Independent Managed Gateway and Worker hardening

### Completed

- Enforced bounded, variant-strict model streams: request/event/stream sizes,
  JSON complexity, Unicode, sequence, identity, continuation and terminal
  framing are validated at both Gateway and Runtime boundaries.
- Made account/model allowlists, monthly quota and active-concurrency admission
  one immediate transaction. Expired leases release concurrency while their old
  execution token remains fenced.
- Strengthened the durable request ledger with cross-account request-ID
  rejection, append-only hash-linked events, atomic terminal commits, exact
  terminal replay and disconnect cancellation without a second Provider call.
- Added lease heartbeats during first-token silence and asynchronous tools;
  restart can resume an idempotent `tool_running` checkpoint, and lease loss
  cancels/fences execution.

### Verification

- Independent focused run: `39 passed`; adjacent Runtime/Capability: `18
  passed`.
- Root reproduction of the combined Gateway, Worker, Job, Runtime and
  Capability surface: `57 passed`.

### Known open work (not complete)

- Production requires an externally anchored audit signature/WORM target,
  account-session composition, shared transactional storage for horizontal
  scale, real Provider deployment and TCP half-close/proxy-buffering E2E.
- Synchronous blocking tools must execute outside the event loop to preserve
  heartbeats. Explicit policy retries intentionally receive a new provider
  attempt identity and may incur another call after an uncertain prior attempt.

## 2026-07-10 - Connector lifecycle authority closure

### Completed

- Added a schema-v5 durable lifecycle request ledger. Auth begin, health,
  disconnect and reauthorization consume stable client request IDs, replay the
  same fingerprint, reject conflicting reuse, lease concurrent ownership and
  persist terminal failure/success.
- Reauthorization is one recoverable vault/database transition: new credential
  first, atomic instance switch, durable old-credential cleanup. Account
  mismatch preserves the existing connection; restart resumes unfinished vault
  cleanup.
- Non-OAuth secret submission remains fail-closed until an OS-vault-only UI
  contract exists. Browser OAuth callback is a nonce-CSP/no-store page using an
  exact loopback parent origin, `postMessage` and safe window close without
  reflecting state, code or token.

### Verification

- Independent quiescent full v1 run at this boundary: `411 passed, 7 skipped`.
- Root Connector reproduction: `50 passed`; Web TypeScript, 19 Node tests and
  production build were also reported green by the domain owner.

### Known open work (not complete)

- The compatibility mount still synthesizes a server request ID when an older
  client omits the lifecycle header; the v1 Web always supplies one. Removal of
  that compatibility path is a GA protocol-cutover task.
- Real Feishu/Tencent tenant OAuth, read/write/revoke and Windows/macOS OS-vault
  tests remain environment gates.

## 2026-07-10 - Deterministic Replay and encrypted local audit outbox

### Completed

- Mock Replay reconstructs a Thread through any valid watermark and fork
  lineage without invoking models, tools, connectors or Artifact writes. Live
  Replay requires explicit confirmation and creates a new Turn/Job after
  replanning current model, capability and permission authority.
- Added OTel-compatible trace projection for thread, turn, model, tool, human
  and Artifact spans without prompts, tool arguments or results.
- Added an AES-256-GCM local audit outbox, OS-vault key reference, redaction,
  leased drain/retry, retention and backfill. Tamper or wrong-key reads fail
  closed rather than dropping the business database.

### Verification

- Root Replay/observability plus Thread-history reproduction: `11 passed`.

### Known open work (not complete)

- A production cloud audit collector with tenant RBAC/admin-access audit and a
  concrete OTLP exporter still has to be deployed and exercised end to end.

## 2026-07-10 - Resumable administrator release promotion CLI

### Completed

- Added the `ecorex-release` entry point and a strict HTTPS Control Plane
  client. It requires an explicit host allowlist, refuses redirects and
  credentialed/non-origin endpoints, bounds JSON in both directions and reads
  the administrator bearer only at request time from a named environment
  variable.
- `ecorex-release promote` validates a signed release manifest and an exact
  evidence map for all 16 mandatory gates, then orchestrates candidate creation,
  gate recording, publication, rollout creation and optional activation.
- A cross-process lock plus atomically replaced promotion journal persists one
  stable request ID per step and the resulting rollout ID. Re-running after an
  ambiguous network failure replays the same server operations and cannot
  create a second rollout. The journal contains no administrator credential.
- Added explicit rollout activate/pause/halt, channel kill-switch and client
  distribution commands so routine release control no longer requires ad-hoc
  deployment scripts.

### Verification

- `7 passed`: strict admin HTTPS client, redirect/auth/contract failure cases,
  resumable promotion journal and existing Control Plane publication flow.
- `py_compile` and scoped `git diff --check` passed; Black is not installed in
  the current workspace and was therefore not claimed as a formatting gate.

### Known open work (not complete)

- A browser administrator release dashboard, production identity provider,
  KMS-backed token issuance and multi-instance signal Pub/Sub remain open.

## 2026-07-10 - Supervised structured retouch in the product Runtime

### Completed

- Bound each retouch request and the unified Durable Job in the same SQLite
  transaction. The worker uses leases, heartbeat, checkpoint, deadline,
  retry/dead-letter and a stable external idempotency key; restart asks the
  image-edit service to recover that key before it can execute again.
- The managed adapter sends structured base/selected/reference revisions,
  normalized annotations and global instruction as bounded HTTPS multipart. It
  has no prompt field and never publishes source bytes or internal annotation
  layers.
- Success atomically creates the new Artifact revision, completed Durable Job,
  public Turn Artifact item, preview metadata, change summary and inspection
  regions. Failure/cancel/recovery converge on matching Runtime and Artifact
  terminal facts.
- Wired the coordinator and Worker supervisor into the formal Runtime lifespan.
  When no managed image-edit adapter is configured, `/retouch` now fails before
  persisting either an Artifact retouch row or an orphan Durable Job.
- Added a backend `retouch_service` readiness projection. The thin WebUI keeps
  the precise-retouch control visible but disabled with the exact backend reason
  instead of silently dropping it or accepting a doomed request.

### Verification

- Domain owner full v1 checkpoint before mainline wiring: `435 passed, 7
  skipped`; retouch focused `9 passed` and Artifact/Runtime cross-regression `93
  passed`.
- Root mainline reproduction: `18 passed, 1 platform skip` across supervised
  Runtime retouch, domain execution, Artifact integration and product server.
- Web TypeScript, `25/25` Node tests, strict Design System gate and production
  content-addressed build passed after readiness/disabled-state integration.

### Known open work (not complete)

- Production still needs the deployed managed image-edit endpoint with durable
  `Idempotency-Key` and `/recover` semantics, plus a real visual edit/compare
  browser E2E against that service.

## 2026-07-10 - Independent ShareSnapshot security closure

### Completed

- Enforced account scope on every local share read/write and validated the
  immutable payload digest, duplicated metadata, status and allowlisted HTTPS
  URL before returning a projection. Concurrent create/publish/revoke, expiry
  and same-timestamp ordering now converge deterministically.
- Public payload construction basename-sanitizes Artifact names and admits only
  user messages plus public Artifact metadata; paths, tool payloads, source
  files, internal layers and binary content cannot cross the boundary.
- Hardened the Runtime publisher to port-443 HTTPS without redirects or
  compression, with an 8 MiB request and 64 KiB response ceiling, exact length
  checks and sanitized exception causality.
- Cloud share tokens remain write-only: only SHA-256 is stored. Snapshot state
  has a keyed MAC and the lifecycle audit is an HMAC chain; every write verifies
  both, while public reads perform constant-time/O(1) state authentication.
  Cross-account use returns NotFound and produces a different public token.
- Added pre-JSON body limits and non-reflective validation errors on the cloud
  endpoint. Revoke, expiry, state/audit mutation and snapshot deletion all fail
  closed.

### Verification

- Independent share-focused run: `28 passed`; independent full v1 checkpoint:
  `440 passed, 7 skipped`.
- Root reproduction including local/cloud share, transport, Runtime list/UI
  contract and administrator Control Plane clients: `35 passed`.

### Known open work (not complete)

- Local Runtime SQLite still stores the complete published URL. If local-at-rest
  token secrecy is mandatory, encrypt it through the OS vault or use an
  authenticated reissue endpoint.
- Crash-stuck publishing/revoking can be retried idempotently but is not yet
  auto-enqueued as a Durable Job. Token-key rotation/versioning, deployment
  disk/KMS encryption, explicit preview-schema migration and large-scale audit
  checkpoints remain GA infrastructure tasks.

## 2026-07-10 - Verified capability packs and executable tool contracts

### Completed

- Added a deliberately bounded JSON-Schema subset at the backend tool trust
  boundary. Every `ToolSpec` is checked when registered; every model-supplied
  argument is validated before its handler is called, and every result is
  normalized and validated before it can enter a Durable Job, event or model
  continuation. Unsupported remote references/keywords fail closed.
- Replaced the built-in tools' catch-all input objects with bounded contracts
  for read, fetch, vision, CDP, shell and image generation. This prevents a
  Gateway response from smuggling undeclared fields into a privileged adapter.
- Added the v1 signed Capability Pack manifest. Pack identity, SemVer, Runtime
  API, platform/architecture, artifact name/size/SHA-256, sorted tool bindings
  and each backend-owned `ToolSpec` digest are Ed25519-covered. Artifact reads
  reject links/reparse points and detect open/read races.
- A verified pack can bind only an exact, callable handler set and cannot
  redefine a ToolSpec, shadow another pack/core handler or claim installation
  without executable adapters. The resulting availability projection marks
  every catalog tool without a real handler as disabled.
- Added a bounded workspace `read` handler. It authorizes explicit roots,
  rejects traversal and links, checks file identity, limits size/chunks and
  returns `workspace://` locators instead of host absolute paths.

### Verification

- `27 passed`: capability planning/invocation/snapshot/Agent Worker regression
  plus signed-pack verification/binding, tamper failure, schema enforcement,
  truthful availability and workspace path confinement.

### Known open work (not complete)

- The verified handler set still has to be wired into the Product Runtime after
  the active managed-session composition lands. Production fetch, vision, CDP,
  sandbox-command and image adapters remain signed deployment packs/providers;
  the Runtime must not advertise them before that binding exists.

## 2026-07-10 - Managed session authority and truthful Product capabilities

### Completed

- Added an Ed25519-signed managed-session lease capped at 72 hours. Account,
  organization, roles, canonical model allowlist, quotas, administrator denies,
  token commitments and monotonic revision are verified on every snapshot and
  bearer read. Plaintext access/refresh tokens exist only in the OS credential
  vault; SQLite stores the signed lease and commitments.
- Session install is a two-stage durable transaction (`staged -> vault_written
  -> committed`) with restart recovery, cleanup journal, stale-request
  fingerprinting, monotonic revision and append-only redacted audit. Logout is
  CSRF/digest/request-ID bound and cannot remove a newer login.
- Product mode now requires the signed session whenever a Model Gateway is
  configured. Only an explicit test-only override permits unmanaged mode. The
  Gateway client must use the same `ManagedSessionService` credential object.
- Bootstrap projects the real identity, organization, roles, lease, quotas and
  admin denies. Models are filtered only by canonical ID; unknown cloud entries
  never become local aliases. Mutations revalidate the lease while expired
  sessions retain scoped read access to local history and Artifacts.
- Product capability composition now builds availability from actual handlers:
  one root-confined `read`, trusted core adapters and Ed25519-verified packs.
  Missing handlers are explicitly disabled. The old Runtime default no longer
  pretends that the image pack is installed.
- Retouch startup requires both the verified image capability pack and managed
  image adapter. In managed mode, the signed lease must also allow
  `gpt-image-2`; bootstrap reports the exact unavailable reason otherwise.

### Verification

- Managed session/Product owner gate: `23 passed, 1 skipped`; adjacent session,
  Gateway, Runtime and Product regression: `78 passed, 1 skipped`.
- Root Product capability/session/retouch convergence: `23 passed, 1 skipped`,
  followed by `10 passed, 1 skipped` after verified-pack readiness fencing.

### Known open work (not complete)

- The cloud identity provider/device-authorization broker and first-login UI
  still need an end-to-end deployment contract; no unsafe local token-import
  endpoint was introduced as a substitute.

## 2026-07-10 - Retouch owns a dedicated backend Turn

### Completed

- A precise-retouch command now atomically creates its own backend-managed Turn,
  completed user message and exactly one `artifact_retouch` Durable Job. It
  never attaches execution state or result items to the source image Turn and
  never creates a competing `agent_turn` Job.
- The backend captures a fresh config/capability/permission/model snapshot for
  the retouch intent and validates it inside the shared Artifact transaction.
  A stale permission, disallowed image model or unavailable image capability
  rolls the entire request back with no Turn, Job or Artifact retouch row.
- Worker success/failure/retry transitions, public Artifact item, preview,
  change summary and inspection regions now belong to that Retouch Turn and
  replay deterministically after restart.

### Verification

- Root convergence across Retouch domain/Runtime/Event Store/Jobs/Replay:
  `28 passed`. The concurrency fixture replays the same request across eight
  initial and eight restarted callers and observes one Turn/message/Job.

## 2026-07-10 - Capability packs enter the signed release graph

### Completed

- Added `capability-pack` as a deterministic ReleaseBuilder artifact kind.
  Every pack ZIP receives a separately signed canonical sidecar containing the
  pack/runtime versions, platform, architecture, artifact size/SHA-256 and
  exact sorted ToolSpec bindings; both ZIP and sidecar are also outer release
  artifacts covered by the release manifest.
- Release construction rejects unknown tools, tools that do not declare the
  named pack, duplicate pack targets, sidecar collisions and over-limit packs.
  The Runtime verifier is the only constructor of a verified-pack proof, so a
  caller cannot instantiate a trusted marker around arbitrary bytes.

### Verification

- ReleaseBuilder/capability-pack/security regression: `25 passed, 1 platform
  skip`; focused pack Runtime plus release round trip: `8 passed`.

## 2026-07-10 - Refresh-safe administrator release console

### Completed

- Mounted the content-addressed, SRI-checked `/admin` Workbench and authenticated
  read-only `/api/v1/admin/resume` route in the formal Control Plane app.
- Candidate, rollout, canary/stable kill-switch and client distribution facts
  are captured in one SQLite WAL read transaction. Explicit latest IDs use
  persisted business time, append-only creation sequence and stable ID tie
  breaking; the page never infers state from array order or memory.
- The console covers candidate creation, all 16 release gates, publication,
  rollout activation/pause/halt, channel kill switches and distribution. Admin
  credentials remain ephemeral and are not stored in browser storage.

### Verification

- Root admin Web/Control Plane/CLI regression: `19 passed`, including app
  rebuild/refresh recovery, authorization and a concurrent commit snapshot
  consistency test.

## 2026-07-10 - ShareSnapshot external effects become Durable Jobs

### Completed

- Share creation and revocation now commit the local Share state, one Durable
  Job, its binding and `job.queued` in the same SQLite transaction. The HTTP
  API returns `publishing`/`revoking`; it never holds a browser request open
  while calling the remote service.
- Dedicated `share_publish` and `share_revoke` workers use lease, heartbeat,
  checkpoint, deadline, retry/dead-letter and stable external idempotency. A
  restart reclaims expired leases without creating a second public snapshot.
- Job payloads contain only the action and `share_id`. Conversation content,
  Artifact paths, account identity, public URL and provider token stay behind
  the Share repository/publisher boundary.
- Expiry or revoke fences an in-flight publish result. A late provider success
  cannot make the URL visible again and causes an idempotent remote revoke.
  Publish/revoke terminal facts and Share terminal state are committed
  atomically.
- The Share supervisor is part of the Runtime lifecycle and is not started
  without a valid managed session. Logout stops it before the same-slot Runtime
  restart.

### Verification

- Share owner focused gate: `36 passed`; adjacent Runtime/Product regression:
  `35 passed, 1 platform skip`; final focused cleanup gate: `10 passed`.
- The post-Share full v1 checkpoint reached `501 passed, 8 skipped`. Later
  device-login/Product/Web changes still require one final quiescent rerun.

## 2026-07-10 - Managed device login and same-slot session reload

### Completed

- Added a durable OAuth-style device flow. SQLite stores only hashed request
  identity and public challenge facts; `device_code`, access token and refresh
  token are confined to the OS credential vault and the trusted broker/session
  boundary.
- The broker transport accepts only an allowlisted HTTPS/443 origin, two fixed
  endpoints, bounded JSON, no redirect and stable idempotency. The returned
  managed lease is still verified by the existing Ed25519 session authority
  before any account/model/policy fact becomes active.
- Polling uses a durable lease and restart-safe attempts. Pending, slow-down,
  authorization, denial, expiry and transient provider failure have explicit
  states; concurrent pollers cannot install the grant twice.
- Added authenticated local routes for start, safe status and poll. They remain
  protected by the Runtime bearer, exact Origin and CSRF, while only these two
  POST routes are exempt from requiring an already-active managed session.
- A successful login requests process exit `86`. The signed Bootstrap
  supervisor re-verifies and restarts the same slot; release activation remains
  the separate exit `85` path and still requires a pointer change.
- Product Server now accepts and identity-checks the device service against the
  exact `ManagedSessionService`; `/bootstrap.login_service` truthfully reports
  whether first-login is available.

### Verification

- Device domain/transport/router gate: `6 passed`.
- Root managed-session/device/Product adjacency: `44 passed, 1 platform skip`.
- Independent Product-to-Runtime black-box convergence: `21 passed, 1 platform
  skip`. It proves login-before-model, local bearer/Origin/CSRF enforcement,
  zero device/token plaintext in API/SQLite/WAL, restart fencing and signed
  identity after app reconstruction. This entry does not claim a production
  identity-provider deployment.

## 2026-07-10 - Thin WebUI interaction and static/Mock GA closure

### Completed

- The React client consumes the expanded managed bootstrap contract, keeps
  model selectors usable before a Thread exists and blocks model/permission
  mutations while unauthenticated. Device login keeps only flow/user-code
  facts in memory, opens HTTPS verification links and refreshes only after the
  backend reports a scheduled same-slot restart.
- One reducer owns terminal thinking cleanup, reconnect/gap recovery, retry,
  queued work and persisted HITL. Interaction controls have a re-entry guard;
  closed mobile navigation is removed from the focus order and the drawer
  constrains keyboard focus.
- Artifact projections replace stale Item state. Fine pointers receive the
  hover/focus action rail; coarse pointers receive a real dialog-backed bottom
  sheet. Retouch renders the returned image, change summary and inspection
  regions rather than a local prompt or text-only success claim.
- Forced-colors, two-ring focus, semantic contrast and the approved
  `WorkspaceSurface`/Workbench system were hardened. The strict CSS gate finds
  no raw color, arbitrary spacing/radius/z-index, `100vw` or `transition: all`
  debt in the four v1 style files.
- Added a same-origin GA Mock Runtime covering bootstrap/CSRF/SSE, first-turn
  terminal, retry/queue, HITL, image2, Artifact/Retouch, unique shares and
  default/full permissions. It is repeatable with `npm run ga:serve` and does
  not replace real-browser evidence.

### Verification

- TypeScript passed; Web tests `39/39`; standalone Mock E2E `2/2`; production
  build passed with `1811` modules and two final-byte content-addressed assets.
- Hallmark static/contrast/design gates and scoped diff check passed. Evidence
  is durable in `.hallmark/log.json` and `.hallmark/browser-ga.json`.
- The Browser integration returned `No browser is available` and inventory
  `[]`. Multi-viewport screenshots, axe, actual keyboard/touch, forced-colors
  and reduced-motion remain explicitly `not_run_no_browser`; no standalone
  Playwright evidence was substituted.

### Discovered contract gap

- Artifact projections advertise `open/reveal`, but no backend intent endpoint
  existed. The WebUI correctly withheld those entries instead of inventing a
  local filesystem action. A backend-authoritative implementation is now an
  active follow-up batch.

## 2026-07-10 - Packaged Product Runtime entrypoint

### Completed

- `ecorex serve --host --port` and `ecorex-product` now start the v1 Product
  Runtime. The command accepts no bearer, API key, config path or install-root
  argument and normalizes errors without echoing native vault/network details.
- Startup is permitted only from the Bootstrap-selected signed current slot.
  It re-verifies the Release/Core receipt, canonical `runtime-config.json`,
  platform/architecture, every path ancestor and the exact signed Web bundle;
  all mutable paths remain confined to the install root.
- The entrypoint composes the OS vault, managed session, device broker, Model
  Gateway and update service. An absent or expired lease starts the local
  unauthenticated shell with model Workers closed, so first login is possible
  without weakening the managed model boundary.
- ReleaseBuilder requires an explicit `product_runtime=True` Core and one
  signed React bundle. It deterministically injects the Web tree/manifest into
  Core and rejects implicit, Web-less or legacy WebChannel/Electron inputs.
- The loader and Runtime lifespan now have single resource ownership. Failed
  dependency construction, repeated Web verification failure and an
  unauthenticated no-Worker shutdown close device broker, update transports and
  Gateway exactly once; the normal Worker path does not double-close Gateway.

### Verification

- Product owner convergence: `114 passed, 4 platform/environment skips`.
- Root entrypoint/Bootstrap/Product/session/Worker reproduction: `49 passed, 3
  platform skips`. Python compilation, help contract and whitespace checks
  passed; only the existing Starlette multipart deprecation warning remains.

### Known composition gap

- A configured Capability Pack is fully verified and requires an exact trusted
  adapter set, but the formal CLI still supplies no production adapter
  resolver. This remains active P0 composition work rather than being described
  as available. Share, Retouch, Audit and stable Connector transports have since
  received signed Product assembly in later batches.

## 2026-07-10 - Backend-authoritative Artifact actions and cloud Audit Product wiring

### Completed

- Artifact `open` and `reveal` are backend intents, not paths sent to React.
  Runtime verifies account, public visibility and the authoritative `actions[]`,
  materializes CAS content atomically into a bounded exports directory, writes
  its event before the external side effect and keeps an at-most-once receipt.
- A crash after launch starts is reported as an unknown outcome and is never
  replayed automatically. Windows and macOS use fixed platform launchers and
  safe argument boundaries; unsupported platforms fail closed. Responses,
  receipts and events never disclose local paths.
- The signed Product Runtime configuration now has an optional cloud Audit
  service with one allowlisted HTTPS/443 ingestion route, bounded dispatch
  cadence and retention no longer than the product defaults. The publisher is
  account-fenced to the exact managed session and has one cleanup owner both
  before ASGI handoff and during Runtime lifespan shutdown.
- WebUI's Artifact More menu sends only artifact/action/request identities and
  renders the Runtime result. It cannot fabricate an open/reveal success or
  bypass an unavailable backend action.

### Verification

- Artifact/Retouch owner gate: `97 passed`; Runtime/permission/action security:
  `42 passed`; Web `40 passed`, TypeScript/build/design gates passed.
- Product Runtime plus cloud Audit composition: `33 passed, 1 platform skip`.
  This covers canonical signed config, fixed endpoints, managed credentials,
  retention bounds and unstarted/ASGI transport cleanup.

### Active follow-ups

- Unified high-concurrency generation/retouch scheduling and Cowart-inspired
  clean-room retouch interaction completeness remain parallel batches. Durable
  reasoning and cross-restart activation health are closed below; packaged
  platform drills and remaining provider capability gates stay open.

## 2026-07-10 - Durable disclosed-reasoning Item lifecycle

### Root cause and completed contract

- The prior WebUI derived a generic “thinking” row only from Turn phase. There
  was no durable reasoning identity, content revision or presentation event,
  so a phase/tool update could make disclosed content disappear before another
  summary existed.
- Added the backend-owned `reasoning` Item and typed
  `reasoning_summary` content. `reasoning.replaced` atomically archives the
  previous visible atom and creates the next one only on its first non-empty
  delta; `reasoning.delta` advances the same Item revision.
- Terminal settlement emits `reasoning.archived` before Item/Turn terminal
  facts. Retry, tool, assistant-content, stream-state and phase events do not
  clear the current summary. Deadline convergence uses the same explicit
  archive operation.
- Managed Gateway events carry a provider reasoning identity and accept only a
  visible provider-approved summary. Hidden chain-of-thought is not stored or
  exposed.
- Event Store projection, fork history, side-effect-free Mock Replay and the
  React reducer implement the same replace/delta/archive semantics. Projection
  resync after a gap or disconnect restores the exact visible atom/revision.
- Timeline renders the backend-projected summary and keeps it visible across
  tool/phase changes. No timeout, animation completion or local phase heuristic
  may hide it.

### Verification

- Focused Runtime/Worker/Replay reasoning tests passed, covering idempotent
  atom revision, atomic replacement, restart reconstruction, explicit terminal
  ordering and managed Gateway validation.
- Web typecheck and all `43` Web tests passed. Reducer tests prove that ordinary
  tool/phase and even a terminal phase fact cannot hide a summary without the
  explicit backend archive event, and projection replay restores revision 5
  exactly.

## 2026-07-10 - Stable managed office Connector Product wiring

### Completed

- Added one strict managed Connector Gateway adapter for Feishu and Tencent
  Docs. OAuth begin/complete, health, actions and revoke use connector-specific
  fixed routes below one signed HTTPS/443 root, reject redirects/compression,
  bound request/response size and recheck the managed session generation around
  every bearer acquisition.
- OAuth returns an opaque `managed_grant`; ConnectorService stores it only in
  the OS credential vault and continues to own PKCE/state, scopes, action
  schemas, idempotency, uncertain-write reconciliation, audit and replay.
- Signed `runtime-config.json` chooses a unique sorted subset of the two stable
  adapters. Unknown/Beta adapters are rejected at composition. Product settings
  identity-check every adapter against the exact managed session, and Runtime
  stops maintenance before closing each async transport.
- Fetch/CDP, vision/image and shell contracts now name `browser`, `image` and
  `sandbox` packs respectively; shell's default policy is workspace-write with
  on-request approval, while full access remains a separate explicit profile.

### Verification

- Managed Connector transport security/contract: `8 passed`.
- Product entrypoint plus connector mount/integration: `33 passed, 1 platform
  skip`. Capability planner/pack/invocation/release adjacency: `20 passed`.
- These are strict mock-gateway and local lifecycle gates. Real Feishu/Tencent
  tenant credentials remain an external GA gate and are not claimed here.

## 2026-07-10 - Product distribution retirement of the legacy Python CLI

### Completed

- The Python distribution now discovers only `ecorex*`. Legacy `cli`, `agent`,
  `channel`, Electron/desktop source and tests are explicitly outside the wheel.
  `click` and `requests` were removed because no v1 module imports them.
- Product console scripts resolve only to Product Runtime, signed Bootstrap and
  Control Plane/release administration. A source-level gate rejects any v1
  import back into legacy packages.
- ReleaseBuilder already rejects `chat.html`, WebChannel, Electron output and
  overlay markers from Product Core/Web inputs; the package boundary now gives
  the same guarantee to ordinary Python wheel construction.

### Verification

- Clean-source wheel: `527244` bytes, `161` members, zero `cli/agent/channel/
  desktop` entries, no Click or Requests metadata dependency.
- Packaging plus ReleaseBuilder/Product adjacency: `42 passed, 2 platform
  skips`. The dirty workspace's pre-existing `build/` directory can contaminate
  an in-place setuptools build, so recorded evidence deliberately uses a fresh
  source staging directory, matching CI/release behavior.

## 2026-07-10 - Backend-authoritative precise-retouch workspace

### Clean-room boundary

- Reviewed Cowart commit `61f6daaf` only for interaction mechanisms. That
  repository exposes no explicit license, so no source, wording, layout or
  styling was copied. EcoreX keeps its existing structured Artifact/Job model,
  Workbench design system and managed image boundary.

### Completed

- Added durable Retouch Workspace rows with optimistic version fencing,
  idempotent coalesced saves, restart recovery, stable annotation identities,
  persisted references/global instruction/view state and submit error release.
- Bound every canvas to an immutable edit surface containing base revision,
  SHA-256, oriented dimensions, EXIF orientation, color space and coordinate
  version. Source, reference and result image endpoints read exact revisions;
  the mutable current-preview endpoint is no longer a coordinate authority.
- Enforced ten reference images in Pydantic, domain and WebUI boundaries and
  pinned each reference revision through the Retouch Job transaction.
- Added deterministic bounded raster masks for rectangle, ellipse, point,
  polygon, polyline and brush geometry. Mask PNGs enter CAS and their digest,
  dimensions, coverage and pixel regions remain backend evidence.
- Rebuilt the thin React editor with all six tools, selection/move/instruction
  edit/delete, bounded undo/redo, pointer-cancel recovery, keyboard submit/Esc,
  pan/zoom/fit, exact-aspect rendering, reference thumbnails and coalesced API
  persistence. No localStorage draft or prompt routing was introduced.
- Submission no longer closes the editor. It shows queued/running/failure
  truth, supports failed-job draft reopening, and presents the completed new
  revision beside the pinned original with compare, inspection overlay, open
  and continue-modifying actions.
- Revision merge logic now uses lineage/time evidence and retains a newer
  completion Item when a delayed Artifact list still contains the base image.

### Verification

- Artifact/Retouch selection: `135 passed, 1 platform skip`.
- Managed image + Workspace/Retouch/Product cross-layer: `45 passed, 1
  platform skip`; CAS mask content is loaded by digest at the adapter boundary.
- Web: typecheck, `49 passed`, production build (`1812` modules/two hashed
  assets) and the six-counter Design System gate all passed.
- Real managed-image quality and multi-viewport browser screenshots remain
  external GA gates; the local mock lifecycle is not substituted for them.

## 2026-07-10 - Multi-origin release asset publication

### Completed

- Added strict resumable publishers for GitHub Releases, the signed domestic
  mirror and the EcoreX CDN. Uploads are digest-fenced, refuse redirects and
  compressed control responses, validate exact public receipt locations and
  never accept a bearer token through command arguments or JSON content.
- Added one publication coordinator with the enforced order: domestic mirror
  ready, GitHub draft assets ready, CDN ready, then optional GitHub visibility.
  A bad mirror/CDN/GitHub receipt stops publication before the public release.
- Added `publish-assets`, backed by one reusable no-secret JSON endpoint config.
  The command verifies manifest/artifact signatures, the exact directory member
  set and every local digest before its first remote mutation. The lower-level
  `upload-github` command is intentionally draft-only.
- Promoting a candidate now also requires the explicit `cdn-sync` release gate;
  GitHub and mirror evidence alone cannot activate a manifest whose signed
  disaster-recovery source is not ready.
- Successful multi-origin publication atomically writes a deterministic receipt
  tied to the release/manifest digest and prints its SHA-256. A crash before the
  receipt is harmless: rerunning reconstructs it from idempotent remote assets;
  a receipt path already tied to another release is rejected.
- Replica receipt validation is split correctly: the transport validates the
  allowlisted HTTPS public host and encoded filename; the coordinator compares
  the complete URL against the signed source base. This supports either a
  version or release-ID directory without weakening manifest authority.

### Verification

- GitHub, replica, coordinator, CLI and Control Plane admin adjacency: `21
  passed`; focused publication/replica/CLI rerun: `12 passed`.
- Tests cover resumable exact assets, tamper rejection before remote mutation,
  mirror-before-GitHub ordering, CDN-before-public ordering, malicious receipt
  rejection, no-secret config construction and deterministic cleanup.
- Live GitHub/domestic-mirror/CDN credentials and origin-outage drills remain
  real-environment release gates; no production release was published here.

## 2026-07-10 - Crash-contained browser/sandbox pack protocol

### Completed

- Added a strict signed-zipapp descriptor and stdio invocation protocol for the
  `browser` and `sandbox` packs. Outer signature/digest, inner pack/tool/runtime
  identity and canonical descriptor must agree before a handler exists.
- Core supplies the effective sandbox, approval, idempotency, execution scope
  and resolved workspace roots. React and model arguments cannot change that
  authority. Child output containing protected host paths is rejected.
- Each call runs outside the Runtime process with a minimized environment,
  fixed system executable search path, isolated Python import mode, bounded
  request/stdout/stderr, deadline, process-group termination and strict
  correlated success/error envelopes. Streams are drained with bounded memory
  after an output flood so Windows pipe transports close cleanly.
- Added and wired the aggregate Product resolver: image tools keep the managed
  in-process adapter, while browser/sandbox select the crash-contained pack
  adapter; unknown pack IDs fail closed. The signed Product loader passes only
  its already-resolved workspace roots, and the packaged CLI always selects
  this aggregate resolver.

### Verification

- Process pack plus existing invocation/runtime adjacency: `15 passed`.
  Covered secret stripping, backend permission propagation, approval,
  idempotency, descriptor mismatch, child crash, protected-path response and a
  five-MiB output flood without an unclosed Windows subprocess transport.
- Aggregate resolver/Product loader integration: `33 passed, 1 platform skip`;
  focused Product/process gate: `25 passed, 1 platform skip`; packaged CLI help
  remains green.

## 2026-07-10 - Cross-restart activation health receipt

### Root cause and completed contract

- Removed production authority from the old Runtime's static candidate health
  callback. Product `InstallCoordinator.activate()` now persists an exact
  provisional activation intent, switches the pointer and stops at durable
  `healthchecking`; it does not mark the slot known-good or complete the
  transaction.
- The intent binds transaction/slot/release/version/build/artifact digest,
  immutable prior pointers, Core payload digest, signed Web bundle digest and
  a storage identity. Bootstrap validates the intent, append-only journal,
  signed manifest/artifact and retained slot receipt through a dedicated path;
  ordinary `CurrentSlotVerifier` remains strict and still rejects every other
  non-known-good current slot.
- Bootstrap launches the candidate with a fresh health nonce in environment
  only. The nonce is omitted from argv, repr, JSON, journal and SQLite. The
  exact loopback health response uses an HMAC proof over the complete identity;
  wrong Host, nonce, identity, proof, media type or response bounds fail closed.
- Provisional Product startup returns a minimal probe-only ASGI composition. It
  verifies signed Runtime/Web/capability inputs and the platform vault type but
  never opens the business database, reads credentials, constructs Provider
  authority or starts a Worker. Every non-health route
  returns `503 activation_health_pending`. A bounded parent/watchdog monitor
  exits an orphaned candidate, and Bootstrap fences a still-occupied port before
  relaunch after its own crash.
- Successful proof makes Bootstrap converge receipt, known-good pointer and
  completed journal under the product lock, then stop the probe and relaunch the
  same slot as the full Runtime. Immediately before any live database can open,
  Product startup durably records the signed storage data barrier; no rollback
  path exists after that point. Prior signed slots are deliberately not pruned
  until this barrier succeeds.
- Pre-data process failure, timeout, spoof, invalid identity, health failure,
  confirmed full-process creation failure or full-process exit restores and
  re-verifies the prior known-good pointers before relaunch. The receipt moves
  through `rollback_pending` to `rolled_back_pre_data`, so a power loss before
  pointer restore, journal append, final receipt or candidate cleanup is replayed
  idempotently. First install returns to an empty current pointer.
- Intent-before-switch, pointer-before-journal, confirmation, Bootstrap probe
  and confirmed pre-data rollback crash windows converge from the signed
  journal/intent/receipt facts. A crossed storage barrier is explicitly tested
  to retain the candidate and require roll-forward repair.

### Verification

- Dedicated activation-health suite: `19 passed`, including RuntimeUpdateService
  restart handoff, signed Product probe-only startup, zero database creation
  before health, mutation gating, nonce non-persistence, first install,
  timeout/spoof, candidate probe/full launch failure, full-process pre-barrier
  exit, parent loss, Bootstrap crash, intent/confirmation/rollback recovery and
  the durable roll-forward storage barrier.
- Update/Bootstrap/Product adjacency: `119 passed, 5 platform/environment
  skips`. Existing download, mirror, signature, recovery, update API, Product
  entrypoint and strict known-good launch contracts remain green.

## 2026-07-10 - Unified managed image Product execution

### Completed

- Added tenant-scoped private input CAS registration and completed-result
  download to the cloud-authoritative `/api/v1/images` API. Result responses
  carry MIME, length, ETag and SHA-256 commitments and never expose storage
  paths; every request resolves ownership from the authenticated principal.
- Added one strict managed image client bound to the exact
  `ManagedSessionService`, signed fixed HTTPS root and immutable account/session
  generation/lease/revision tuple. Its local restart journal stores identities
  and fingerprints only. A known request always recovers its cloud Job before
  submit, so an uncertain response cannot become a second provider request.
- Mapped structured Retouch surface, normalized annotations, deterministic mask
  evidence and content digests to one cloud Retouch Job. Base/reference/mask
  bytes are read from Artifact CAS, verified and uploaded by digest; neither
  local paths nor prompt-routing instructions cross this adapter.
- Bound signed `imagegen` and `vision` pack contracts to product-owned handlers.
  imagegen and Retouch share the same client and scheduler. Missing pack/client
  state remains visible as an exact disabled reason instead of falling back to
  the legacy image script.
- Deleted the former synchronous multipart/base64 Retouch gateway adapter,
  credential protocol, package exports and endpoint-specific test. A static
  product-source gate now prevents that adapter, endpoint or base64 result
  contract from returning.
- Implemented two-phase local image publication. The cloud job/result
  commitment is durable before the Artifact, then one lineage-bearing Artifact,
  inline Item and event are published idempotently. Unique cloud-result and
  Artifact-marker indexes plus restart marker recovery prevent duplicate
  deliverables without re-running the provider.
- Added a renewable publication lease across cloud polling, result staging and
  Artifact CAS. A replacement token fences the old owner; heartbeat loss is a
  retryable product error. Protocol-invalid missing result descriptors now use
  a stable error code and release the lease rather than relying on `assert`.

### Verification

- Managed image integration covers tenant fencing, strict download validation,
  restart recovery, shared generate/Retouch scheduling, structured mask
  mapping, Artifact crash recovery, lease heartbeat concurrency and malformed
  result settlement.
- Image/Product/Pack/Retouch cross-regression commands and exact counts are
  recorded in `docs/v1.0/verification-ledger.md`.
- The suites use a real ASGI API, SQLite WAL, private CAS and deterministic
  provider double. Deployed PostgreSQL/object storage load, real managed image
  quality and Windows/macOS packaged lifecycle remain external GA gates.

## 2026-07-10 - Rotation-safe public ShareSnapshot authority

### Root cause and completed contract

- Replaced the Control Plane's single Share HMAC key assumption with a bounded
  `CloudShareKeyRing`: exactly one key issues new identities, while explicitly
  retained old keys verify and revoke existing snapshots.
- Persisted an immutable non-secret key ID and MAC version with each token
  digest/snapshot state and audit entry. MAC v2 binds the key ID itself; a
  database edit cannot silently relabel a row to another valid key.
- Kept published URLs stable across rotation. Idempotent republish derives the
  token from the snapshot's original key, not the current active key; revoke
  recomputes the state MAC with that same original key, and expiry remains an
  immutable payload fact.
- Made the append-only audit chain rotation-aware. A single chain can contain
  old- and new-key entries without re-signing history, while missing historical
  verification keys stop further mutations rather than bypassing audit.
- Added an atomic pre-keyring schema annotation path. Historical token, state
  and audit MACs remain version 1; the upgrader temporarily suspends only the
  audit UPDATE trigger inside one immediate transaction, attaches the explicit
  legacy key identity, restores the append-only trigger before commit and emits
  only key-bound version 2 records afterward. A populated multi-key migration
  without an explicit legacy identity is rejected; a cryptographically wrong
  explicit choice rolls the entire schema annotation back before commit.
- Retained the one-key constructor as a compatibility boundary. No key material
  or key ID is exposed through public URLs or Share projections; an unavailable
  row key is returned publicly as not-found and remains a sanitized conflict on
  authenticated mutation paths.

### Verification

- Focused Control Plane Share rotation and existing cloud-share contract:
  `12 passed`.
- Local Share/Control Plane full adjacency: `40 passed`; covers unique URLs,
  Durable Job publish/revoke recovery, transport hardening, rotation, old URL
  idempotence, cross-key audit, revoke, expiry, removed-key failure and legacy
  schema migration.
- Wider `create_control_plane_app` adjacency: `34 passed` with only existing
  third-party deprecation warnings; release, cloud audit and real TLS update WSS
  contracts remain compatible.
- The active/retired key lifecycle still requires deployment KMS/HSM custody and
  an operational rotation drill; those are environment gates, not inferred from
  the in-process cryptographic contract tests.

## 2026-07-10 - Publication and process-pack adversarial hardening

### Root causes and completed contract

- Closed the signed-pack time-of-check/time-of-use gap. Browser and sandbox
  zipapps are re-hashed against their signed manifest immediately before child
  creation and again before a successful result is accepted. Canonical JSON
  now rejects non-finite values, excessive graph complexity and malformed
  request values with stable pack errors.
- Replaced Windows parent-only termination with an actual process-tree kill
  (`taskkill /T /F`) followed by a direct-kill fallback. A real regression
  creates a pack parent and grandchild and proves neither remains active.
  Protected-path comparison now normalizes Windows separators, and the child
  environment is rebuilt with canonical allowlisted variable names.
- Made the multi-origin publisher fail before its first remote mutation when
  signed source URLs, publisher identities, CDN availability, local filenames
  or digest maps disagree. Mirror and CDN hosts are strict HTTPS identities;
  replica receipts reject bool-as-int fields and uploads re-hash the still-open
  source file after transport.
- Bound existing GitHub tags to the exact EcoreX release body/name/channel.
  Concurrent tag or asset creation (`422`) now resumes only by re-reading the
  exact tag or same-name SHA-256 receipt; a conflicting asset remains
  immutable. GitHub asset URLs are repository/tag/name fenced, and publication
  must be explicitly confirmed as non-draft.
- Extended the publication receipt lock across release identity validation,
  every remote mutation and atomic receipt persistence. Two administrator
  processes can no longer publish different releases and discover the shared
  receipt conflict only after both have changed remote state. Windows reparse
  entries, reserved/duplicate release filenames and non-regular JSON inputs are
  rejected.
- Added checked-in, no-secret
  `release/v1/publication-config.example.json` and its strict companion schema.
  All sample hosts use the reserved `.invalid` suffix; credentials remain
  late-bound environment-variable names. The CLI parser and schema/example
  property sets are regression-bound.
- Promotion now requires the exact publication receipt. It verifies the
  release/manifest identity, non-draft GitHub state, all three signed source
  URLs, every asset size/digest and cross-origin byte equality. The
  `github-release`, `mirror-sync` and `cdn-sync` gates must all contain the same
  canonical `publication-receipt:sha256:<digest>` evidence token. An unrelated
  receipt cannot release a candidate through the product CLI.
- Control Plane repository publication is monotonic: repeating publication
  with a new request identity preserves the original `published_at`; direct
  repository gate evidence also observes the same bounded size contract as the
  HTTP model.

### Verification

- Focused Process Pack and existing pack runtime: `17 passed`, including a
  real Windows descendant-tree termination, post-binding artifact mutation and
  non-finite response rejection; trusted system-tool resolution is independent
  of parent `SystemRoot`/`WINDIR` values.
- Publication transports/coordinator/CLI: `31 passed`, including GitHub
  creation races, signed-source preflight, cross-release receipt locking,
  replica type fencing and checked-in config/schema binding.
- Control Plane publication/release CLI: `18 passed`, including immutable
  publication time and same-receipt three-gate evidence.
- ReleaseBuilder/Web/Capability/Control Plane combined adjacency: `109 passed,
  2 platform skips`.
- Live GitHub, domestic mirror/CDN, platform pack sandboxes and signed packaged
  Windows/macOS process drills remain external release gates.

## 2026-07-10 - Durable managed OTLP/HTTP JSON trace export

### Root cause and completed contract

- Closed the gap between the existing read-only OTel-compatible projection and
  a production transport. The Product Runtime now composes a concrete managed
  OTLP/HTTP JSON exporter from signed, credential-free configuration and owns
  its transport through the ASGI lifespan.
- Added a separate encrypted trace outbox rather than overloading Audit
  records. A terminal Turn or archived Thread marks an immutable segment in the
  source Event transaction. Restart backfill, deterministic batch identity,
  SQLite leases, expired-lease recovery, bounded exponential retry with jitter
  and `Retry-After`, terminal rejection and retention are backend facts.
- Export is completion-oriented: a terminal Turn emits only its Turn/model/tool/
  human/artifact subtree; the Thread root is emitted on archive. This avoids
  duplicate full-thread snapshots and does not expose unfinished spans.
- OTLP payloads use lower-camel proto3 JSON fields, hexadecimal trace/span IDs,
  integer enums and decimal-string Unix nanoseconds. Batches are bounded by
  Span count and canonical JSON byte length; local paths, credentials and
  binary-shaped data are redacted before AES-GCM persistence.
- The collector endpoint is exactly allowlisted HTTPS/443 `/v1/traces`, refuses
  redirects/compressed responses and fences every request to the same managed
  account/session generation. A non-zero OTLP `partialSuccess.rejectedSpans`
  becomes a permanent diagnostic instead of a false successful delivery.
- No OpenTelemetry SDK/protobuf dependency was added. The JSON wire contract
  follows the stable OTLP/HTTP protobuf JSON mapping while reusing the existing
  Runtime TraceProjector and local audit-key authority.

### Verification

- Dedicated exporter/outbox suite passes `15` tests covering fixed endpoint identity, canonical
  wire shape, size limits, partial success, Retry-After, terminal-only
  projection, redaction, encryption, retry and crash/expired-lease recovery.
- Signed Product focused tests pass `2` cases covering canonical configuration
  round-trip, invalid route and size rejection, exact managed-session ownership
  and ASGI-owned transport closure. Runtime lifecycle ordering stops the
  dispatcher before closing its transport.
- OTLP, Cloud Audit, signed Product, Server and Replay adjacency passes `66`
  tests with `2` platform/environment skips.
- A real external collector, production RBAC/retention dashboard and sustained
  offline-to-online soak remain explicit deployment gates.

## 2026-07-10 - Deterministic Replay thin-WebUI exposure

### Root cause and completed contract

- The Runtime already implemented deterministic Mock Replay and explicitly
  confirmed Live Replay, but the v1 WebUI had no task-level entry. Diagnostics
  therefore existed only as a backend contract and users could not inspect the
  verified watermark/projection or safely request a new Replay Turn.
- Added a task Header More menu entry and one focused Replay dialog. Mock Replay
  is visibly read-only and presents only Runtime-returned integrity evidence:
  source/through watermarks, event count, SHA-256 digest, task summary, Turn,
  Item and interaction counts. A failed refresh retains and labels the last
  verified snapshot instead of replacing it with unverified local state.
- Added backend-projected `live_replay_turn_ids`. This closes the fork-lineage
  authority gap: React no longer treats every visible terminal Turn as
  executable, while the Runtime continues to validate the source at mutation
  time.
- Root integration preserved Capability pipeline separation required by Live
  Replay: a missing pack still yields `eligible=false` and hidden exposure,
  while the immutable decision retains current-policy `requires_approval`.
  Availability cannot silently erase governance evidence before a later pack
  install or Replay permission comparison.
- Live Replay requires a source selection, an explicit danger checkbox and a
  separate action. Copy explains that current permissions are re-evaluated,
  prior approvals are not inherited and historical external side effects are
  not reused. One request identity survives ambiguous network failure and
  dialog reopen through the Runtime session owner.
- Once accepted, the session refreshes the normal Thread projection. The dialog
  names the exact new Turn and permission snapshot, and the same Turn appears
  in the ordinary timeline; no parallel frontend execution model was added.
- Extended the GA Runtime with a `replay` scenario. Mock reads do not move its
  sequence, confirmation is required, duplicate Live requests return the same
  Turn, and no model/tool/connector implementation is present.

### Verification

- Backend Replay suite: `8 passed`; Thread catalog/kernel/Replay adjacency:
  `14 passed`. Capability plus Replay governance adjacency: `16 passed`.
- Web typecheck passed. Full Web contract suite: `56 passed`, including Replay
  transport, reducer request stability, stale-snapshot Live fencing,
  backend-only candidate mapping and GA Mock/Live idempotency.
- Production Web build transformed 1814 modules and emitted two
  content-addressed assets. The strict design-system gate kept all six
  prohibited debt counters at zero; scoped `git diff --check` passed.
- A real Browser instance is unavailable, so keyboard, touch, forced-colors,
  reduced-motion, screenshot and axe evidence remains a named external gate;
  static/GA results are not represented as visual E2E.

## 2026-07-10 - Quiescent local v1 convergence

### Completed

- Stopped every implementation writer and removed four stale, task-owned
  Control Plane pytest processes before starting the release-like gate. No
  source mutation occurred during the authoritative full-suite run.
- The first full run exposed three stale test assumptions that treated a shell
  handler as executable without its signed `sandbox` pack. Product behavior was
  not loosened: HITL tests now install/bind an explicit test pack, and catalog/
  policy tests distinguish `missing_packs:sandbox` from later admin denial.
- Preserved Capability pipeline separation: unavailable tools remain hidden and
  ineligible, while immutable decisions retain the current governance
  `requires_approval` fact for diagnostics and Live Replay.
- The second complete Python run passed. The final React run, production build,
  Design System gate, Python compileall, Product/Release CLI imports, JSON scan,
  tracked diff check and new-source trailing-whitespace scan also passed.

### Verification

- Full Python v1 suite: `673 passed, 8 skipped`, zero failures. Skips are named
  platform/environment conditions already represented in the GA matrix.
- Web: typecheck, `56 passed`, 1814 transformed modules, exactly two final
  content-addressed assets and all six design-debt counters at zero.
- Static convergence: all `ecorex`/v1 tests compile; 7 engineering JSON files
  parse; Product and Release CLI help surfaces remain closed; no pytest/Node
  test process remained; no trailing whitespace in v1 source/docs.
- Local code gates are now quiescent. Real Browser, provider credentials,
  Windows/macOS signed candidates, live origins/collector and production data
  drills remain explicit external GA gates rather than inferred success.

## 2026-07-10 - Durable multi-instance Control Plane update hints

### Root cause and completed contract

- Replaced the single-process-only rollout notification path with a durable
  append-only signal/outbox in the existing Control Plane transaction. Rollout
  activate, pause and halt, channel kill and kill-clear all commit a signal only
  when their canonical mutation, idempotency receipt and administrator audit
  commit. A channel kill records one bounded affected-rollout fact per halted
  rollout plus the channel state fact.
- Added monotonic `AUTOINCREMENT` signal sequences, stable event IDs, request-
  bound dedupe keys, update-forbidden rows, bounded batch reads, per-instance
  monotonic consumer cursors and time retention that always preserves a latest
  floor. Retention never resets sequence identity. A lagging consumer detects a
  gap and processes the retained committed suffix; facts that were already
  removed recover only through the periodic signed feed, never a synthetic WSS
  event.
- Every ASGI app owns and closes one bounded asynchronous poller. Production can
  supply a stable unique instance identity explicitly or through
  `ECOREX_CONTROL_PLANE_INSTANCE_ID`; independent instances sharing the database
  consume the same facts with separate cursors. Backoff and bounded batches
  contain database/transport failure, while the five-minute feed poll remains
  available.
- Kept the public WSS schema unchanged. Signals are wake-up hints only. Active
  rollout hints are resolved against current eligibility; pause/halt hints use
  the original rollout targeting only to wake affected clients, which then
  re-read the canonical signed feed. Signal rows and payloads contain no actor,
  account, organization, token or secret material.
- Preserved low-latency same-instance behavior even for explicit ASGI embeddings
  that disable lifespan. The request path locally fans out the exact committed
  stable event ID; the durable poller may replay it, and the existing Runtime
  signal repository makes that boundary idempotent.
- Removed the legacy `broadcast_rollout` helper and the synthetic gap-resync
  event path. Before any WSS fan-out the Hub now requires the complete model to
  match the same durable row by sequence; a forged event ID or altered field
  fails before entering a client queue. Static and behavioral tests bind that
  the Hub cannot manufacture rollout identity.
- Bound kill-switch wake ordering end to end. One transaction appends each
  affected `rollout.halted` fact before `channel.killed`; a second ASGI instance
  consumes that order and sends the targeted halted-release wake hint to an
  already connected/staged client. Its immediate signed-feed refresh returns
  no release after the kill, so the hint accelerates revocation without becoming
  authority.

### Verification

- Dedicated multi-instance suite: `8 passed`, covering node-A mutation to a
  node-B WSS client, exact activate -> targeted halt -> channel-killed ordering,
  tenant targeting, same-request signal idempotency,
  crash-before-ack replay, stable-instance restart without redelivery, missed
  WSS feed recovery, retention gaps, monotonic post-prune sequence identity,
  immutable rows, transaction rollback, fail-closed instance identity and
  kill/clear signal composition.
- Focused signal/Control Plane/real-WSS/Runtime update chain after the authority
  closure: `33 passed, 1 environment skip`; every public hint event ID is now
  traced to a committed outbox row.
- Control Plane/Update adjacency: `72 passed, 1 environment skip`, including
  existing rollout/feed/RBAC behavior, real TLS Uvicorn WSS delivery, Runtime
  durable signal deduplication, periodic feed polling, Share, cloud Audit and
  administrator client/dashboard contracts.
- The local tests use two ASGI instances and independent repository objects over
  one WAL database. A deployed multi-replica database/identity/termination soak
  and network-partition drill remain release-environment evidence.

## 2026-07-10 - Unified Extension platform architecture batch (historical boundary)

### Root cause and fixed acceptance boundary

- Product inspection found several independent extension authorities: legacy
  `plugins/`, legacy `agent/skills` and MCP loaders, the v1 Tool registry,
  Capability Pack bindings and the Connector registry. Separate identity,
  enablement, trust, health and upgrade rules make the apparent management
  disorder structural rather than a menu-layout problem.
- v1 will own one durable backend Extension registry for `skill`, `mcp_server`,
  `tool_provider`, `capability_pack` and `connector_provider`. Connector
  instances remain in their own business domain, while the adapter package and
  its executable lifecycle can no longer bypass Extension governance. The
  immutable identity is contract
  version plus extension ID, semantic version and artifact digest; source,
  signature/trust, compatibility, dependencies/conflicts and exported
  capabilities are validated before an installed revision can be activated.
- Lifecycle facts are backend-owned and restart-safe: staged, installed,
  enabled, disabled, quarantined, superseded and rollback state must be
  idempotent and auditable. Unknown kinds, contracts, permissions, dependencies
  or health responses fail closed. UI toggles submit intent and render the
  returned projection; they never import code, invent availability or bypass
  policy.
- Executable MCP/tool-provider/pack code may not hot-load into the Runtime
  process. It must run behind the existing bounded process and managed-session
  boundaries with restart budget, circuit/quarantine state and exact
  permission/capability snapshots. Skills remain declarative content and may
  reference only registered capabilities.
- The legacy directories are migration inputs only. Their configuration may be
  inventoried and staged for explicit revalidation, but old loaders and
  arbitrary command arguments cannot remain a production execution path.

### Historical pending evidence (superseded)

- This was the pre-implementation acceptance boundary. The later Extension
  end-to-end closure, immutable Turn snapshot, crash containment, generated
  contracts and current-tree checkpoints supersede its former pending status.

## 2026-07-10 - Tracked v0.3 Web executable cutoff

### Root cause and completed boundary

- Removed all `66` tracked files below `channel/web/`: the WebChannel handler
  graph, `chat.html`, copied React bundles, v0.29 overlay CSS/JS, console code,
  logos, fonts and vendored browser libraries. The deletion did not touch the
  untracked `agent/core/` experiment, migration fixtures, user data, ignored
  release evidence or installed updater slots.
- Removed the four source-era Web artifact builders/checkers:
  `check-ecorex-web-release.sh`, `prepare-ecorex-web-release.ps1`,
  `prepare-ecorex-webui-local-release.ps1` and
  `validate-ecorex-release-artifacts.py`. These scripts packaged the deleted
  WebChannel/static tree and cannot describe a v1 Core/Web artifact.
- Replaced `app.py`, root `run.sh`, `scripts/run.ps1` and `scripts/start.sh`
  with exit-78 tombstones and removed the obsolete provider-key-bearing
  `config-template.json`. `channel/channel_factory.py` no longer registers or
  imports a `web` channel. No legacy launcher can silently assemble or start a
  source Runtime; the signed Bootstrap/Product entrypoints remain separate.
- Added `scripts/check-v1-legacy-cutoff.py`. It rejects retired source/build
  paths, a non-tombstoned source launcher and any absolute import from v1 back
  into `agent`, `channel`, old CLI/provider/plugin/tool roots. The gate ignores
  only generated `__pycache__` and `.pyc` files, which are excluded from source
  and signed artifacts; the separate release scanners still validate the exact
  Core and hashed Web allowlists.
- Added a black-box test that proves `channel.web.web_channel` has no import
  spec, `app.py` exits 78 without starting a service, and importing
  `ecorex.migration` does not load a legacy Runtime module. Migration continues
  to inventory and parse data copy-on-write; it does not use old executable
  source. Rollback continues through signed known-good slots.

### Verification

- Static cutoff gate: pass.
- Cutoff, Product packaging and copy-on-write migration focus: `18 passed`.
- Final strict cutoff plus ReleaseBuilder/Web bundle/capability-pack/migration/
  package/design adjacency: `76 passed, 2 named platform skips`.
- PEP 517 isolated wheel: `605396` bytes, `174` entries and zero
  `agent/channel/cli/desktop/chat/overlay/Electron` members. The preceding
  no-build-isolation attempt failed only because the host Python lacked the
  `bdist_wheel` command; using the declared isolated build requirements passed.
- Historical v0.x Web tests and smoke programs are not treated as v1 evidence;
  their exact disposition is audited separately instead of weakening the v1
  gate or restoring the deleted implementation.

### Historical-test and CI disposition

- Deleted five implementation-bound tests after migrating the two still-current
  contracts into v1: `tests/test_ecorex_web_parallel_backend.py`,
  `tests/test_web_runtime_goal.py`, `tests/test_v029_webui_followups.py`,
  `tests/test_v028_runtime_queue_observation.py` and
  `tests/test_v030_webui_hardening.py`. The v1 replacements assert progressive
  Artifact actions for hover/focus/coarse pointers and non-exclusive image
  planning that keeps read/fetch/vision/CDP/shell discoverable.
- Removed `.github/workflows/ecorex-desktop-release.yml` and
  `.github/workflows/ecorex-webui-macos-smoke.yml`; both invoked deleted
  Electron/WebChannel packagers. Added `testpaths = ["tests/v1"]` so the
  default pytest/CI authority is explicit. A final collect-only run found `698`
  nodes and zero outside `tests/v1`.
- Tombstoned `scripts/release-ecorex-default.ps1` and
  `scripts/release-ecorex-webui-orchestrator.ps1` at exit 78. After its only
  current LF-byte requirement was migrated into the v1 ReleaseBuilder tests,
  removed `scripts/prepare-ecorex-public-release.ps1` with the rest of the old
  production packagers. The strict cutoff now passes with no exception.
- Deleted `17` additional pure v0.x release/deploy/install/Web smoke programs,
  including the old public packager, target deploy/rollback drivers, release
  artifact validators, production Web acceptance programs and the desktop
  release-manifest updater. Removed `docker/entrypoint.sh`,
  `scripts/install-ecorex-web.sh` and the old `ecorex-web.service` example so a
  Docker/service/install command cannot start `app.py` after WebChannel removal.
- Retained reference inventory (historical, not v1 CI): three read-only/mixed
  baseline/memory/capability smoke programs, mixed legacy provider/connector tests and
  the old public download-site manifest. Residual reference files fell from
  `33` to `16` after excluding the v1 rejection tests/scanner itself. These
  references are not represented as completed work and must not be selected by
  a v1 release job. Static download-site migration remains a separate local
  batch and does not re-enable a Runtime or packager entrypoint.

## 2026-07-10 - System stability, responsiveness and comprehension batch (historical boundary)

### Root-cause acceptance boundary

- State-machine stability will be assessed as one system rather than by green
  unit suites in isolation. Turn/Item, Durable Job, Interaction, Install,
  Extension, Memory reset and outbound facts must define commit points,
  idempotency, restart reconstruction, lease/fencing and cross-domain
  causation. Fault injection must cover failure before commit, after commit but
  before delivery, duplicated delivery, stale revision and process restart.
- High-frequency model output is not allowed to turn SQLite, SSE or React into
  a token-by-token bottleneck. The backend must preserve exact text and ordered
  facts while coalescing bounded deltas, apply backpressure and flush before
  terminal facts. The client must batch stream application by render frame,
  update only affected Items, preserve scroll intent and avoid layout shifts or
  whole-timeline live-region announcements.
- Capability/Tool progressive disclosure remains backend-owned. Image intent,
  Skill/MCP discovery and explicit aliases may rank or defer tools but cannot
  erase unrelated eligible capabilities. Invocation rechecks current provider
  revocation, permission and idempotency authority even when a Turn owns an
  older immutable discovery snapshot.
- The default user output location is an export policy, not the Artifact CAS.
  React selects a backend-projected safe location alias; raw host paths never
  become browser authority. A location change affects future work through an
  immutable output-policy snapshot and cannot redirect an in-flight Turn.
- “Reset memory” means learned/user memory only. Factory knowledge, product
  policy, conversations and Artifacts are not deleted. The mutation is
  confirmed, idempotent, audited and transactionally tombstones the selected
  revision with an explicit recovery/purge boundary.
- Thread IDs become visible/copyable product identities. Starting from an ID
  in a new task uses the backend Thread catalog/fork contract, preserves
  lineage and snapshots, and never asks the model to fabricate or rediscover
  history from prompt text.
- System observability must cover event-loop lag, process/resources, SQLite/WAL
  contention, queue/lease depth, SSE connections/backpressure, model TTFT and
  stream rate, tools, connectors, Artifact/CAS, memory, update and Extension
  health. Metrics are bounded/redacted and export through the managed trace/
  audit boundary; a local diagnostic projection uses user language and keeps
  codes/IDs in an opt-in technical-detail section.
- Product copy will translate phases, permissions, retries, tools and failures
  into concise Chinese actions and outcomes. Raw tool IDs, sandbox names,
  provider errors and internal state codes do not appear in primary chat text;
  they remain available for diagnosis without losing audit precision.

### Historical pending evidence (superseded)

- This was the pre-implementation acceptance boundary. Later Runtime invariant,
  bounded streaming/media, Output, memory reset, Thread continuation,
  observability, plain-language UI and browser batches supersede the queue note;
  real platform/provider/soak evidence remains separately listed in the GA
  matrix.

## 2026-07-10 - Public Bootstrap discovery and download-site cutoff

### Release authority and atomic pointer

- Added `ecorex.release.public_index` as the only producer of the browser-facing
  discovery document. It accepts only a verified stable manifest, exact raw
  manifest bytes, the exact canonical publication receipt and a trusted public
  key set. It independently checks the raw-byte digest, reparses those bytes,
  verifies the manifest and exactly three platform Bootstrap signatures, and
  proves all three origins contain identical asset identities.
- Hardened the release CLI's JSON/manifest reader to use a bounded regular-file
  descriptor with before/open/after identity checks. The live pointer writer
  validates its complete runtime shape, enforces 256 KiB, takes the product
  lock, fsyncs a same-directory temporary and atomically replaces the old file.
  The persistent lock is keyed outside the served tree; a failed validation or
  replace does not change the prior pointer or expose a lock artifact.
- Added the strict Draft 2020-12 schema with exact object widths, canonical
  64-byte Ed25519 base64, safe IDs/filenames, aware RFC 3339 timestamps, three
  ordered source kinds and HTTPS-only URLs. Runtime validation, JSON Schema and
  browser parsing are independent fail-closed layers.

### Static public surface

- Removed the obsolete v0.3 `manifest.json`, `release-index.json`, Windows/macOS
  `install-webui` scripts and executable Web service example. The two installer
  deletions were confirmed as clean whole-file deletions relative to the
  baseline; ignored local downloads and dirty admin dashboard files were not
  opened or rewritten by this batch.
- Replaced the old website behavior with a v1 Bootstrap-only page. Its
  checked-in pointer is canonical `unpublished`/`release:null`, so a source
  checkout cannot claim a release exists. When a real pointer is published,
  the page validates exact keys/types, retries mirror -> GitHub -> CDN and
  compares the exact manifest response bytes with the published SHA-256 before
  rendering any download link. The copy continues to state that only
  Bootstrap's embedded Ed25519 keys authorize installation.
- Public JS, CSS and all three images are named by the first 12 hexadecimal
  characters of their real SHA-256. HTML references only those names. Caddy
  and Nginx examples serve HTML/discovery with `no-store` and content-addressed
  assets/downloads with one-year `immutable` caching.
- Added the idempotent public-asset builder. It materializes new digest names
  before atomically switching HTML, then removes old/generated crash leftovers,
  so an interruption before or after the mutable pointer change remains
  recoverable without a broken asset reference.
- Added `scripts/check-v1-public-download-site.py` to reject old manifests,
  installers, unhashed/misnamed assets, unresolved references, fake checked-in
  URLs/signatures, missing manifest-byte verification and incomplete cache
  policy. The release runbook records the one-command pointer build and the
  browser-vs-Bootstrap trust boundary.

## 2026-07-10 - Cross-domain Runtime crash consistency and event-loop isolation

### Root causes closed

- Reproduced the final-lease split brain: a `running` Agent Job reached
  `dead_letter` while its Turn stayed `streaming` and the scheduler allowed the
  same Thread's next Job to pass it. Lease reclamation now writes the Job fact,
  projection, Turn retry/terminal fact, Item settlement and HITL closure in one
  transaction. Unsafe provider/tool phases explicitly enter `retry_wait`;
  exhausted attempts fail the graph. If `model.response_completed` and
  `finalizing` committed before the crash, recovery completes the graph even
  when that was the last attempt.
- Reproduced Python SQLite's implicit pre-commit: calling
  `Connection.executescript()` inside `SQLiteDatabase.transaction()` preserved
  an earlier insert after the caller raised and rolled back. Runtime now uses a
  transaction-safe Connection implementation that parses complete SQLite
  statements, preserves the caller transaction and rejects nested transaction
  control. This closes the same hidden boundary in Runtime snapshots,
  permissions, sessions, Update state/events, observability outboxes and the
  unified Extension repository.
- Agent Turn and Runtime Update background loops previously called synchronous
  SQLite repositories on the ASGI asyncio thread. Every repository call in the
  hot worker/update paths is now dispatched off-loop. Durable lease fencing is
  unchanged; WAL/busy-timeout contention no longer freezes model-stream
  keepalives and unrelated requests.
- A later event could explicitly supply a different immutable snapshot/trace
  identity because EventStore filled only missing fields. EventStore now
  compares every supplied config/capability/permission/Extension/trace identity
  with `turn.accepted` and rejects drift before appending.

### System invariant and fault harness

- Added `RuntimeInvariantAuditor`, which uses one WAL read snapshot plus SQLite
  quick/foreign-key checks. It replays Turn, Item, Job and Interaction lifecycle
  facts and checks monotonic heads, contiguous sequences, references, snapshot
  identity, projection state, complete lease tuples, attempts, waiting/retry
  coupling and terminal dependents. Reports contain only codes, states and
  durable IDs; no prompt, tool argument, path or secret. It never mutates the
  evidence it diagnoses.
- Added commit-fault tests that raise `BaseException` from the transactional
  event sink during Turn creation and terminal settlement, reopen the database,
  and prove zero partial facts/projections. Additional tests inject projection,
  lease and snapshot drift; exhaust/reclaim leases; recover terminal response
  facts; and hold SQLite calls while proving the asyncio loop remains live.

### Verification checkpoint

- New invariant/fault suite: 11 passed.
- Agent worker, supervisor and invariant convergence: 21 passed.
- Runtime/Permission/Session/Extension/Update adjacency: 144 passed, 4 skipped;
  one stale raw `events` column fixture was then updated for
  `extension_snapshot_id` and its focused rerun passed.
- Retouch/Share/Artifact Job adjacency: 40 passed.
- Runtime Update/activation/composition/transport adjacency after offload:
  40 passed, 1 environment skip.
- Extension fence + Agent worker + Runtime composition: 25 passed, 1
  environment skip.
- Audit/OTLP/Replay/Capability/Connector/Artifact repository adjacency: 127
  passed.
- Copy-on-write Migration + Product Runtime entrypoint + server composition:
  39 passed, 2 environment skips.

The immutable-candidate full suite remains a root-level convergence gate after
the other active performance/memory/output/thread-observability batches land;
these focused results do not replace it.

## 2026-07-10 - Unified Extension platform end-to-end closure

### Backend authority and provenance

- Replaced separate Skill/MCP/tool/connector/pack toggles with one Extension
  registry over the Runtime WAL database. Immutable revision rows, detached
  signature evidence, quarantine facts, catalog snapshots, lifecycle events and
  idempotency responses are separately append-only. Revision identity uses the
  unsigned canonical declaration plus artifact digest, so key rotation records
  new re-verifiable evidence without changing product identity.
- Added exact Core declarations for base tools, every loaded Capability Pack and
  the connector adapter layer. Product Server composes them only after the Web,
  Release and Core build are verified; the same signed build digest binds each
  declaration. Publisher/administrator manifests use the current Ed25519 trust
  provider. Export IDs are checked against the exact backend Tool, Connector and
  Pack registries. MCP accepts and records only stable protocol `2025-11-25`.
- Added a static-only `local_bundle` Skill source. ZIP uploads and internal
  administrator directory imports share one normalizer: 10 MiB total, 256
  files, 2 MiB per file, bounded path depth/inventory and compression ratio;
  canonical NFC/portable paths; duplicate/case collision, traversal, link,
  reparse/device, executable and compression-bomb rejection. Root `SKILL.md` is
  strict UTF-8 with a closed, flat whitelist frontmatter. Scripts, hooks, bin,
  command, environment, secret, network and native-code namespaces/formats are
  rejected. Files receive individual SHA-256 identities and an atomic canonical
  CAS manifest. `local-content-sha256` is deliberately integrity evidence, not
  publisher trust.
- Migration imports only bounded legacy Skill metadata under deterministic
  `legacy.skill.*` identities. It cannot load `SKILL.md`, a script or MCP
  command, and both enable and Runtime bind paths permanently reject
  `legacy_import` revisions until a new bundle passes the v1 contract.

### Turn/runtime and thin Web integration

- `extension_snapshot_id` now travels with config/capability/permission/model
  facts through `TurnSnapshotContext`, accepted/derived events, durable Job
  context, Live Replay and the TypeScript event contract. Runtime verifies the
  immutable catalog digest and its config binding inside Turn admission.
- New-Turn availability uses only enabled, healthy, dependency-complete and
  currently provenance-valid provider revisions. Immediately before the actual
  tool call, Agent worker re-checks the historical Turn snapshot against the
  current provider revision; disabling, replacing, quarantining, dependency
  loss, signature revocation or CAS corruption therefore fences already queued
  work as well as future disclosure.
- Bootstrap and `GET /api/v1/extensions` return the same generated projection.
  Enable, disable, health and rollback accept expected revision plus stable
  request identity. The local Skill endpoint accepts canonical base64 ZIP only,
  never a host path. React renders those actions/reasons, uploads a bounded ZIP
  through the Runtime client, and keeps all import/enable decisions in the
  backend. Product Server supplies the exact platform/architecture, release
  verifier and Core build identity to the service.

### Verification checkpoint

- Extension provenance/CAS/lifecycle/API/MCP/Turn-fence suite: 15 passed, 1
  platform symlink skip.
- Runtime kernel/composition/permission/Agent worker/Replay plus Extension:
  46 passed, 1 platform skip.
- Product Server/Product Runtime/Extension composition: 42 passed, 3 named
  platform/environment skips.
- Web TypeScript check passed. The first 69-test Web run found one mobile-first
  CSS contract omission in the newly added import row; the token-only responsive
  rule was added before the final Web rerun recorded in the verification ledger.

## 2026-07-10 - Plain-language Web boundary and diagnostic disclosure

- Added one frontend language boundary for service reasons, request failures,
  Artifact families and human-readable file sizes. Unknown backend codes no
  longer become primary UI copy; status-aware guidance is shown instead.
- Startup, local connectivity, model availability, permission mode and update
  activation now describe the user-visible state and next action without
  exposing Runtime, sandbox or approval implementation names. The persistent
  permission badge still makes Default versus Full Access visible and explains
  how to revoke Full Access.
- Connector account failures, device-login failures and Share publication
  failures now keep the friendly explanation in the primary reading path.
  Bounded error codes, service reasons and Share IDs remain available through a
  keyboard-accessible, collapsed `TechnicalDetails` disclosure; Replay and
  diagnostics retain their full technical evidence.
- Artifact rows and the full-window preview now use Chinese office-family names
  and compact IEC-style sizes. Precise-retouch unavailability uses the same
  reason mapping in hover, menu and touch-sheet paths instead of exposing a raw
  service literal.
- Clipboard denial still cannot report success. The Share dialog now instructs
  manual selection without misclassifying a browser permission denial as a
  Runtime outage, and task rename/continue guidance describes the user outcome
  rather than event-store mechanics.

### Verification checkpoint

- `npm run typecheck`: passed.
- `npm run test:v1`: 72 passed, including three new language-boundary tests.
- `npm run build`: passed and content-addressed both production Web assets.
- `python scripts/check-v1-design-system.py`: passed with zero hard-coded
  radii, shadows, colors, numeric z-index, layout transitions or
  `transition: all` findings.

## 2026-07-10 - System-level health projection, streamed rendering and task continuity

- Added a bounded Runtime health service and supervisor over the shared SQLite
  WAL store. It records event-loop responsiveness, SSE lifecycle, process and
  storage pressure, queue/Turn/Job/HITL/image/retouch state, Artifact and Memory
  volume plus aggregate Worker/Connector/Extension/Update health. Sampling runs
  in a worker thread, provider failures degrade only their component, health
  transitions are append-only and old samples are pruned to a fixed limit.
- Instrumented the actual SSE generator at connect/event/close boundaries and
  retained adaptive active-versus-idle polling. The React session batches only
  delta facts to one animation-frame dispatch, flushes state/terminal facts
  synchronously, stops on the first sequence gap and now applies `item.delta`
  rather than dropping streamed assistant text. Completed messages use bounded
  content visibility while live text avoids expensive pretty reflow.
- Settings now presents four plain-language system components and loads the
  redacted metric projection only inside an explicit “技术详情” disclosure.
  Primary health responses never contain raw metrics. The GA Runtime models the
  same split contract so browser evidence cannot silently accept a missing
  production endpoint.
- Added backend-authoritative task continuation by full `thr_…` identity and a
  copy-ID action. A new task can read its saved projection/events and continue;
  clipboard denial reveals the ID for manual copy and never reports success.
- Image preview now opens as a viewport-inset workspace with the complete image
  fitted at zoom 1. Magnifier controls remain available for 125–400% inspection
  and “显示完整图片” deterministically returns to fit-to-window.

### Verification checkpoint

- System service/API/SSE integration: 5 focused tests passed; adjacent Runtime
  hardening/kernel API set passed 31 tests.
- Web typecheck passed; the v1 Web suite passed 72 tests before adding the
  system transport/GA assertions, whose focused rerun then passed 20 tests.
- Public Bootstrap site static gate passed and public discovery/release/control
  adjacency passed 30 tests.

## 2026-07-10 - Backend-authoritative default Output locations

- Added one account-bound Output service over the same Runtime/Artifact SQLite
  database. The browser sees only `documents`, `downloads` and `workspace`
  aliases, revision and availability; OS/product composition resolves the host
  roots and no API projection contains an absolute path.
- Every Turn config snapshot now includes the current immutable
  `output_policy_snapshot_id`. Artifact saving resolves the creating Turn's
  frozen policy, so changing Settings affects only new tasks. The user action
  is now “保存到默认位置” and returns a durable, path-free receipt plus a visible
  alias/display-name confirmation rather than invoking the browser's unrelated
  download directory.
- Materialization streams and verifies the Artifact CAS, rejects internal and
  implementation families, fences root replacement/symlink/reparse attacks,
  uses same-directory fsync and exclusive publication, stabilizes concurrent
  name collisions, and recovers interrupted `preparing/published` receipts.
  Identical content is reused; different content never overwrites it.
- Settings exposes the default location and clearly states that an active long
  task keeps its original destination. Preference mutations use expected
  revision and stable request identity; stale responses refresh before retry.
  The GA Runtime implements the same preference/materialization contract.

### Verification checkpoint

- Output domain: 14 passed, 1 Windows account capability skip.
- Runtime integration: 1 passed, proving two Turns on opposite sides of a
  preference change materialize into their independently frozen roots and the
  receipt contains no path.
- Web typecheck and complete v1 suite: 80 passed after Output transport,
  Settings and GA contract integration.

## 2026-07-11 - Thin Web feature loading and bundle regression budget

- Replaced eager imports for Settings, Extension management, Share, Replay,
  full Artifact preview and Precise Retouch with `React.lazy` feature islands.
  The first open activates the download; the loaded subtree remains mounted on
  close so dialog drafts and local state retain their existing behavior.
- Added a shared Suspense and error boundary with plain-language loading,
  feature-local failure containment and refresh recovery. Pointer/focus intent
  warms menu-driven features without preloading them in the production HTML.
- Preserved all Settings system-health, learned-memory, permission and update
  props while moving the component behind its lazy boundary.
- Kept the release asset graph acyclic without weakening content addressing.
  Rollup uses stable third-party Runtime, EcoreX API/state and UI primitive
  layers; it has no feature-name or icon-name manual chunk table.
- Added a post-rehash bundle gate and focused contracts. Production now fails
  if the entry, aggregate initial JavaScript, gzip initial JavaScript or any
  chunk exceeds its budget, or if a low-frequency feature is accidentally
  module-preloaded.
- Recorded exact before/after output in `docs/v1.0/web-bundle-report.md`.
  Workspace entry fell from 517.68 kB to 54.22 kB; initial JavaScript is
  452.51 KiB / gzip 140.10 KiB, with 64.13 KiB / gzip 22.47 KiB deferred.

### Verification checkpoint

- `npm run typecheck`: passed before the production build checkpoint.
- `npm run test:v1`: 80 passed, including six new lazy/budget contracts and
  the concurrently integrated output-location transport contract.
- `npm run build`: passed; 11 content-addressed Web assets, no chunk above
  500 KiB and no Vite large-chunk advisory.
- Browser focus, first-open loading and slow/failing chunk behavior remain part
  of the final GA browser matrix rather than inferred from Node-only tests.

## 2026-07-11 - Session, Connector and Extension async-boundary hardening

- Audited every asynchronous entry point in `ecorex/session`,
  `ecorex/connectors` and `ecorex/extensions`. The common root cause was not
  SQLite itself, but synchronous repository, credential-vault, file-verification
  and callback work being invoked directly by ASGI handlers or supervisors.
  Under WAL contention or a slow OS credential provider those calls stalled
  unrelated streaming and input handling on the one Runtime event loop.
- Device authorization now performs request lookup, secret storage, poll-lease
  acquisition, signed-session installation and terminal/retry commits in worker
  threads. Lease acquisition and its audit fact remain one transaction. Broker
  calls are bounded; the supervisor polls at most four due flows concurrently,
  uses a bounded per-flow deadline and leaves an expired lease for deterministic
  restart recovery instead of inventing an in-memory retry state.
- Connector auth, health, invocation, disconnect and uncertain-operation API
  paths now move repository, vault, policy, audit and outbox work off the event
  loop. Adapter calls use one Runtime-loop limiter (16 by default) and a bounded
  deadline. A timed-out synchronous provider keeps its limiter slot until its
  worker actually exits; write calls are persisted as `uncertain` and therefore
  cannot be silently retried as success or failure.
- Connector recovery/outbox maintenance has independent non-blocking cycle and
  publisher locks. One stuck synchronous event sink can occupy only one worker;
  later maintenance ticks do not accumulate threads, and shutdown is bounded.
  Pending delivery no longer runs inside Product construction; the
  lifecycle-managed supervisor owns initial and recurring drain. Delivery
  remains at-least-once from the already committed immutable outbox.
- Managed connector transport reads the managed-session generation and bearer
  token in one off-loop snapshot sequence before network I/O. Extension enable
  and health-check preflight/verification plus their transactional commits now
  run off-loop; synchronous or asynchronous health probes share an eight-call
  limiter and a 20-second default deadline. Probe timeout records the safe
  `health_probe_timeout` fact and never activates the candidate revision.
- The public `/api/v1` request/response contracts and existing SQLite
  transaction/lease/audit facts are unchanged. Only scheduling, deadlines and
  resource bounds changed; no frontend or Runtime API module was edited in this
  batch.

### Verification checkpoint

- Device, managed-session, Connector contract/persistence/composition/gateway,
  Runtime Connector mount and Extension platform adjacency: 102 passed, 1
  platform symlink skip after refreshing one signed-bundle fixture with the
  now-required Core build digest.
- New deterministic regression coverage proves event-loop responsiveness during
  delayed SQLite/session reads, bounded Device supervisor timeout and restart
  reclaim, Connector write uncertainty after provider timeout, serialized stuck
  outbox work, and Extension synchronous-probe responsiveness/timeout fencing.
- Scoped `compileall` for Session, Connector and Extension production modules
  passed.
- Product Server, Runtime composition, system observability and Worker
  supervisor adjacency passed 17 tests with one named environment skip.

## 2026-07-11 - v1 CI and cross-runner byte stability

- Added the read-only `EcoreX v1 CI` workflow. The authoritative quality job
  runs pinned Python/Ruff/pytest, full v1 tests, compileall, a clean Node 22
  install, high-severity npm audit, TypeScript/Web tests, the content-addressed
  production build and all static v1 gates.
- Added an explicit compatibility matrix for Windows x64, macOS arm64 and
  macOS x64. The current fixed labels are `windows-2022`, `macos-15` and
  `macos-15-intel`; ADR-107 supersedes the original mutable Windows alias after
  GitHub's 2026 move to VS 2026. These jobs run platform-sensitive Runtime
  smoke tests, Web typecheck/build and the same byte gate. They neither sign nor
  publish.
- Added `scripts/run-v1-lint.py` as the cross-shell lint entrypoint. Ruff checks
  the complete v1 Runtime/test/gate surface for syntax, undefined-name,
  duplicate-definition and import-structure failures. Existing public
  re-export/unused-symbol debt is an explicit baseline exception rather than a
  reason to omit lint from CI.
- Added a canonical reproducibility contract. It validates the checked-in
  unpublished pointer byte-for-byte, verifies SHA-256 prefixes in public and
  WebUI JS/CSS names, rejects CR/CRLF in v1 shell and digest-bearing text, and
  records sorted path/hash/size facts without host, timestamp or architecture.
  CI compares four manifests byte-for-byte after build.
- Expanded `.gitattributes` with LF policy for shell, HTML, JS/CSS, JSON,
  Python, TOML and workflow sources. Dev dependencies now pin pytest, Ruff,
  `jsonschema` and `python-multipart`; a new venv proved the test suite does not
  inherit those packages from the developer machine.

### Verification checkpoint

- Clean Python venv installed only `.[dev]`; pinned tool imports succeeded,
  lint passed, and CI/public-index/Product Server/Runtime-entry tests passed 41
  with 2 named environment skips.
- Clean `npm ci` found zero vulnerabilities at the high threshold; Web
  typecheck, 87 tests and the 11-asset content-addressed build passed.
- Design, legacy cutoff, public download and reproducibility gates passed;
  workflow YAML parsed and the edited scope passed `git diff --check`.
- GitHub-hosted matrix execution remains unclaimed until Actions runs. Signed
  WebUI Runtime archives, SBOM/license/secret scans, publication and Runtime
  install/update/rollback evidence remain release-environment work; native app
  installers and notarization are outside scope.

## 2026-07-11 - Complete image preview and browser focus closure

- The Artifact row/media card is the preview trigger. Opening an image no
  longer starts from a cropped or magnified surface: zoom `1` is the explicit
  fit-to-window state, the preview canvas fills the viewport-inset dialog and
  the image uses `object-fit: contain`. The magnifier remains available for
  bounded 125–400% inspection and “显示完整图片” returns to the fitted state.
- The full-window preview keeps backend-authoritative saving (`Artifact ID` +
  revision through Output materialization); it never creates a browser-side
  download path. Closing the Radix dialog returns focus to the exact Artifact
  card that opened it.
- A real in-app Browser run at 1280×720 measured the fitted body at about
  1213×505 px with equal canvas/image bounds and no horizontal overflow. At
  125%, the canvas expanded to about 1498 px and became scrollable; resetting
  restored the 1213 px fitted width. Evidence is stored under
  `docs/v1.0/evidence/image-preview-fit-current.jpg`.
- First-open Settings exposed a second root cause: a trigger can remain
  connected while its mobile Sidebar is hidden, so `isConnected` alone is not
  a valid focus-restoration test. Shared restoration now rejects body,
  document, disabled, inert and hidden candidates, verifies
  `document.activeElement` after focusing, and walks visible task-menu and
  navigation fallbacks.
- Suspense loading/error surfaces now use Radix Dialog rather than a visually
  modal `div`. They trap focus, support Escape/overlay close, expose real title
  and description semantics, and delegate final focus restoration to the same
  application contract.
- Added a dedicated Artifact preview contract covering card-click activation,
  default fitted state, bounded zoom, viewport-sized canvas and `contain`
  rendering so future CSS changes cannot silently reintroduce cropping.

### Verification checkpoint

- Browser: Settings close returned to the Settings trigger; preview close
  returned to the original Artifact card; zoom/readjust behavior matched the
  measured geometry above.
- `npm run typecheck`, `npm run test:v1` and `npm run build`: passed with 87
  Web tests, 11 content-addressed assets and the bundle gate at 55.05 KiB entry,
  454.64 KiB initial JS / gzip 140.61 KiB.
- Design, legacy-cutoff and public-download static gates passed. The design
  scan still reports zero hard-coded radius, shadow, raw color, numeric
  z-index, layout-transition or `transition: all` findings.
- The complete responsive/theme/touch/forced-colors/reduced-motion/axe matrix
  remains release-candidate evidence; this browser checkpoint is deliberately
  recorded as partial rather than promoted to that full gate.

## 2026-07-11 - Full-suite timing determinism

- The first current-tree full Python run reached 759 passes and 10 named skips
  but exposed two load-dependent test-harness failures. Neither was dismissed
  as an intermittent product failure.
- The state-machine fixture had frozen `datetime.now()` during collection and
  issued a 120-second lease from that old value; late-suite completion therefore
  expired a newly constructed test lease. Each test now creates its own
  explicit clock and lease recovery advances from the committed expiry.
- A Windows `spawn` lock test used a fixed five-second readiness wait. It now
  uses a bounded 20-second monotonic deadline, reports PID/exit/alive state on
  failure and guarantees join → terminate → kill cleanup without hiding the
  original assertion.
- The two files pass together (19 passed, 2 platform skips), adjacent Job/Update
  coverage passes (42 passed, 2 skips), and the two Windows spawn cases pass in
  three consecutive runs. Product assertions and skip policy were not relaxed.
- After all writers stopped, the authoritative full `tests/v1` run collected
  778 tests and finished with 768 passes, 10 named platform/environment skips
  and zero failures in 355.75 seconds. This is the current local source-tree
  checkpoint, not evidence for the still-unbuilt signed platform candidate.

## 2026-07-11 - Fixed-viewport responsive GA browser harness

- Added a test-only, same-origin responsive wrapper to the standalone GA
  Runtime. Its canonical matrix covers 1440×900, 1024×768, 768×900 and
  390×844 in both light and dark themes without adding a viewport or theme
  branch to the production React application.
- Each wrapper owns an iframe with exact CSS-pixel width and height and reports
  the frame's real `innerWidth`/`innerHeight`, applied theme, horizontal content
  overflow and the presence/visibility of navigation, model selection,
  composer, task type and Artifact controls. The report is also exposed as
  `window.__ECOREX_GA_VIEWPORT_REPORT__` for deterministic in-app Browser
  inspection.
- Preserved the production document's `frame-ancestors 'none'` contract. A
  separate `/__ga/frame-app` response alone uses same-origin framing and an
  external, pre-module theme bootstrap; ordinary `/` and the wrapper remain
  unframeable.
- Viewport, theme and scenario inputs are exact allowlists with duplicate and
  unknown parameter rejection. Unknown `/__ga/` paths cannot fall through to
  the SPA, and the fixed wrapper source cannot recurse or accept an injected
  frame URL. All matrix, wrapper, frame and helper-asset responses are
  `no-store` under route-specific CSPs.
- Added `responsive-ga-harness.md` as the durable operator/browser handoff.

### Verification checkpoint

- Focused GA tests: 5 passed, including CSP separation, 8-entry matrix,
  content-addressed frame bundle, pre-module theme setup and malicious/
  duplicate/recursive parameter rejection.
- `npm run test:v1`: 89 passed with no regression to Runtime transport,
  reducers, Artifact/retouch, Replay, extension, design, lazy-feature or bundle
  contracts.
- The subtask's in-app Browser backend was unavailable (`agent.browsers.list()`
  returned no browser), so this checkpoint does not claim rendered matrix
  screenshots. The harness is ready for the root session's real-browser matrix
  run; Node route tests are deliberately not substituted for that evidence.

## 2026-07-11 - Real responsive/theme/axe matrix closure

- The root in-app Browser executed all eight exact viewport/theme entries from
  the GA harness: 1440×900, 1024×768, 768×900 and 390×844 in light and dark.
  Every frame reported the requested CSS viewport, zero horizontal overflow,
  the requested theme and visible navigation/model/composer/task/Artifact
  controls.
- Added GA-only axe-core 4.12.1 loading under the isolated frame CSP. The
  production document remains non-frameable and never loads axe. All eight
  frames finished with zero violations and zero incomplete checks.
- The browser audit found product defects rather than merely generating a
  report. The workspace page title now has an `h1`, model and Artifact action
  clusters expose group semantics, compact Sidebar icon buttons retain
  accessible names, and the Composer label can no longer create horizontal
  overflow.
- The exact JSON report and eight light/dark screenshots are stored under
  `docs/v1.0/evidence/`. The report is the authoritative measurement; stitched
  full-page screenshots are visual evidence and not substituted for the
  per-frame DOM facts.

## 2026-07-11 - Signed Windows WebUI Runtime candidate drill

- Built a real Windows x64 Core archive containing Python 3.11 Runtime closure,
  ASGI product code and the exact content-addressed React build. An ephemeral
  Ed25519 key signed the ReleaseBuilder manifest and artifacts; private key
  bytes were never persisted and the drill made no external publication.
- Exercised first install through `awaiting_user → activating → healthchecking
  → completed`, then a separately signed same-version fault candidate through
  pre-data rollback. The original slot was restored, relaunched and returned
  authenticated `/api/v1/bootstrap` HTTP 200; the failed slot was discarded
  and the product registration pin was released.
- The drill exposed and repaired real activation defects: signed payload
  mutation from Python bytecode, user-site dependency leakage, environment-
  dependent Windows architecture detection, readiness probes that treated an
  expected unauthenticated 401 as failure, and unbounded/error-revealing
  startup diagnostics. Bootstrap now disables bytecode and user-site imports,
  derives Windows bitness from the signed process, obtains the bearer only
  through the injected no-store page contract and reports fixed safe stages.
- This is a WebUI-serving Runtime archive, not an Electron/native desktop app,
  EXE installer or OS application-signing claim. The evidence is
  `docs/v1.0/evidence/windows-x64-signed-candidate-drill.json`.

## 2026-07-11 - Bounded browser media and long-timeline rendering

- Replaced eager per-thread media downloads with an abortable preview cache:
  viewport-near loading, four concurrent requests, 24 entries, 64 MiB total,
  revision-aware deduplication, LRU URL revocation and explicit rejection of an
  oversized single preview. Thread/revision changes cancel stale work.
- Added a second request fence at the dialog boundary. A slow preview response
  can no longer overwrite a newer Artifact preview or repopulate a dialog the
  user already closed.
- Long tasks now start with an anchored 120-message DOM window. History pages
  have stable item anchors, so incoming streaming deltas do not move the page;
  users can page newer/older or return to the live tail. Completed rows retain
  native `content-visibility`, while `item.delta`/reasoning deltas are batched
  once per animation frame with a 50 ms background-tab bound and terminal or
  state facts flush synchronously.
- The pre-contract-integration checkpoint passed TypeScript, 97 Web tests,
  content-addressed build and the design-system gate. Dedicated cache,
  timeline, preview-race, forced-colors, reduced-motion, coarse-pointer and
  clipboard-denial contracts are now part of the source gate set.

## 2026-07-11 - WebUI-only scope hardening

- Reconfirmed ADR-006/013 as the current delivery scope: React WebUI plus the
  local backend Runtime and signed online-update archives. Electron, native
  windows, DMG/native apps and their application signing/notarization chain
  are not v1 deliverables.
- Removed the remaining tracked macOS app entitlements, DMG installation note,
  native icon build inputs, dead `window.ecorexDesktop` bridge declarations and
  `build:renderer` compatibility alias. The legacy cutoff gate now rejects
  those native-app inputs if they return.
- Root/WebUI README, CI wording and release runbook now call platform outputs
  WebUI Runtime archives and distinguish Ed25519 archive verification from OS
  application signing. Windows/macOS names remain host compatibility targets,
  not desktop UI products.

## 2026-07-11 - One administrator WebUI and one release authority

- Removed the public site's v0.3 static admin, zero-dependency Admin API, usage
  panel and their server install/check/migration scripts. They implemented a
  second SQLite release state, Basic Auth, old client keys and legacy
  `/message`/`upload`/public Runtime routes beside the v1 Control Plane.
- The download page now links to `/admin/`. Caddy and Nginx proxy only
  `/admin*` and `/api/v1/admin*` to the loopback v1 Control Plane; they never
  publish a user's local Runtime. The Control Plane's content-addressed admin
  assets, in-memory bearer and backend role checks remain authoritative.
- Public downloads now advertise ZIP/GZip/BIN Runtime archives only; DMG/EXE
  MIME and native-app routes are absent. The old `/ecorex-agent/admin` path is
  a redirect, not a second application.
- Moved the retired-host redirect out of inline HTML into the hashed public
  module, then regenerated the content address. Public HTML has one external
  module and no inline script/style/base; Caddy/Nginx add no-store, nosniff,
  no-referrer and a self-only script/style CSP with HTTPS connect permission
  for the three signed manifest origins.
- The strict public and legacy gates now reject every removed admin/API/usage
  tree, legacy proxy snippet and old installer entrypoint if it returns.

## 2026-07-11 - v0.3.0 copy-on-write migration closure

- Audited the actual v0.3.0 release-data implementations at `f0750d24` and the
  final local image-hotfix commit `9ac3b958`. The seven schema/state source
  blobs are byte-identical. The repository has no `v0.3.0` tag and its release
  target, release-index commit and package hashes disagree, so the migration
  report now separates schema compatibility from marker metadata and explicitly
  records that the historical archive was not attested.
- Added strict read adapters for `agent_runs`, `agent_run_events`, queued request
  payloads and scheduler JSON v1. Conflicting IDs/owners, invalid JSON,
  unsupported schema versions and malformed schedules abort staging. Runtime
  events are redacted diagnostic history; they are not injected into the v1 UI
  event stream.
- Removed the last source-side SQLite ambiguity: even `mode=ro` can touch a WAL
  shared-memory file on some platforms. Migration now copies stable DB/WAL
  bytes into staging, re-hashes the source pair, and invokes SQLite only on the
  private copy. A live-WAL fixture proves uncheckpointed rows are imported while
  source DB/WAL/SHM bytes and mtimes remain unchanged during the run.
- A matching legacy request enriches its imported Turn with model and terminal
  state. Active states become `interrupted`, while a branch is linked only when
  child/parent request ownership and its fork event boundary can be proven.
- Active/queued message work becomes a bounded, redacted
  `requires_user_confirmation` recovery draft. Legacy schedules remain disabled
  pending confirmation. Neither path inserts a v1 Durable Job, so Runtime start
  cannot execute v0.3 hidden context, tools, connectors or schedules.
- Staged the permission profile as `default` or `full_access` for later account
  binding. Remembered grants and filesystem paths are counted but not activated.
  External permission and release marker files can be pinned into the same
  before/after digest under opaque labels.
- Matched the released WebUI hydration behavior: cached `sessionUiState`
  messages are imported only for sessions without canonical DB history.
- Evidence and provenance limits are recorded in
  `docs/v1.0/evidence/v030-migration-baseline.json`.

## 2026-07-11 - Signed declarative candidate storage migrations

- Candidate Core archives now carry a canonical `storage-migrations.json`
  whose plan hash participates in the build digest and Ed25519-signed Artifact.
  Admission accepts an explicitly newer target schema while rejecting
  downgrade; the candidate Runtime itself still requires its compiled schema
  to equal the signed target.
- Migration input is a bounded declarative operation set, never candidate SQL
  or Python. Admission and live preflight run copy-on-write against the same
  plan hash, record source/target digests, row counts, `quick_check`, foreign
  keys and release/build identity, and reject links/reparse points or receipt
  drift. A poison candidate proves admission does not execute candidate code.
- Live preflight runs before the data barrier and live apply after it. Failure
  before new-data use rolls back; failure after the barrier enters explicit
  roll-forward repair rather than pretending an unsafe downgrade is possible.

## 2026-07-11 - Shared image storage and bounded cloud workers

- The image orchestrator now supports PostgreSQL 15+ leasing with explicit,
  validated migrations and S3-compatible shared CAS. PostgreSQL cannot be
  paired with node-local CAS; SQLite remains the local correctness/reference
  mode only.
- Shared blobs use create-if-absent writes, digest/MIME verification, ETag CAS
  reference mutation, tombstones and conditional deletion. Deletion can resume
  after a crash and refuses to remove bytes whose ETag changed. Blocking S3 and
  database calls leave the ASGI event loop.
- Upload chunk limits and a 512 MiB worker memory envelope derive 1–8 execution
  slots. PostgreSQL pools are bounded to 1–16 connections and fail closed when
  their schema, trigger or index contract drifts. Real PostgreSQL/S3 load is
  still a candidate-environment gate, not inferred from deterministic fakes.

## 2026-07-11 - Generated Runtime contracts

- Bootstrap, Event, Artifact-list and Artifact-detail boundaries are generated
  from authoritative Pydantic schemas into deterministic JSON Schema and TS
  metadata. SSE `id`/`event` headers must agree with the durable envelope; an
  unknown enum, missing field, count drift or bad digest is rejected before it
  reaches React state.
- Artifact families and visibility no longer have a second frontend allowlist,
  and historical malformed Artifact Items are omitted rather than cast through
  `unknown`. Contract generation has a pinned full-schema digest and a CI
  `--check` mode.

## 2026-07-11 - Public ShareSnapshot v2 and Codex-density WebUI

- ShareSnapshot v2 binds each visible Artifact to its Turn and carries only an
  immutable raster-media descriptor. The local durable worker reads the
  verified Artifact CAS, uploads each image sequentially with a stable media
  idempotency key, then publishes JSON. Missing or mismatched media prevents a
  public snapshot; retry repeats the same media key.
- The Control Plane stages PNG/JPEG/WebP/GIF/AVIF bytes under account/share/
  media authority, with 16 MiB item, 64 MiB share and four-request memory
  bounds. A token can read only media declared by its active, unexpired
  snapshot; revoke/expiry immediately return 404. Fresh orphans are capped per
  account and old unlinked bytes are reclaimed, while linked media is
  immutable. SVG, redirects, digest/MIME/magic drift and cross-account reuse
  fail closed.
- The public page is a script-free chat transcript. It labels user intent as
  “你的指令”, Agent output as “EcoreX”, attaches the image to the correct Turn,
  renders it with `contain`, and keeps the full-image link under the same token.
  Schema-v1 canonical bytes and old text-only links remain compatible.
- Ordinary WebUI text/icon/tool buttons now have transparent idle border,
  background and shadow; hover/focus/active alone reveal the low-contrast
  surface. Primary, dangerous and selected controls retain semantic treatment.
  System UI and `ui-monospace` stacks, 14/22 body, 13/20 controls and 12/16
  captions are locked by CI; chat/connector/office rows use sparse framing.
- Browser inspection found two cascade/order defects missed by source scans: a
  broad `font: inherit` overrode 13/20 controls, and equal timestamps caused
  opaque Item IDs to reorder a reply before its instruction. The shorthand was
  narrowed to font family, backend projections now order by first Event `seq`,
  and React preserves projection/event insertion order.

### Browser and focused verification checkpoint

- Main WebUI at 1280×720 measured ordinary idle controls at transparent border
  and background with 13px/20px type. The conversation DOM order was `你 →
  EcoreX`; the media Action Rail was non-interactive at rest.
- The real Control Plane share route loaded a 1800×1100 image as 758.66×463.63
  with `object-fit: contain`, zero horizontal overflow and zero scripts. At
  390×844 it rendered 364.65×222.84, kept zero overflow and switched the
  Workspace to radius 0. Evidence is
  `evidence/share-chat-browser-audit.json`.
- The browser run first rejected an obsolete short GA CSRF fixture, proving the
  generated boundary failed closed. The fixture now uses the same minimum
  token contract and has a regression assertion.

## 2026-07-11 - Core storage schema authority closure

- Removed the last Runtime-owned pre-GA extension-column `ALTER TABLE` path.
  A running Runtime can create the current core schema only in a database with
  no user tables; any non-empty store must already carry the exact compiled
  storage version.
- Core tables, indexes and safety triggers now have one canonical definition
  fingerprint. Missing version metadata, an incomplete event/job-context
  column set, a deleted index or trigger, or a same-name altered trigger fails
  before Runtime DDL and remains unchanged on disk. This prevents
  `CREATE IF NOT EXISTS` from masquerading as repair or migration.
- Product composition and its Output tests now establish the versioned core
  database before Artifact/Output repositories register their domain tables.
  A reversed order is treated as an unversioned partial store rather than
  silently adopted.
- Candidate storage evolution remains the Ed25519-bound declarative plan with
  copy-on-write admission, live preflight, data barrier and receipts. Existing
  pre-GA compatibility ALTER paths inside some non-core domain repositories are
  explicitly a remaining consolidation batch; they are not represented as
  closed by the core fingerprint work.

## 2026-07-11 - Production Control Plane composition and operator lifecycle

- Added `ecorex-control-plane serve`, `schema migrate/check`, and `backup
  create/check`. Configuration is environment-only and secret material enters
  through a fixed `SecretProvider`; the CLI has no secret, token, key, DSN or
  path arguments and emits no exception text.
- The built-in provider declares and enforces single-node SQLite WAL: one
  cross-platform process lock, one persistent-volume marker, encrypted-volume
  attestation, bounded free-space checks, distinct verified backup storage and
  exactly one replica. `postgresql`/multi-replica input fails before storage and
  a typed provider seam records the later HA boundary without a fake fallback.
- `serve` validates the three existing schema authorities and repository audit
  chains but never executes DDL. Explicit migration takes the instance lock,
  creates a pre-upgrade backup, runs core/Audit/Share migrations, verifies WAL
  and creates a post-upgrade backup. A partial new database is removed; a failed
  existing upgrade restores the verified pre-migration SQLite copy.
- Production Share always receives `S3ShareObjectStore`. Startup/check require
  an HTTPS SDK client, encrypted private bucket controls and a private
  write/head/delete probe. SDK credentials remain in the workload-identity/
  boto chain. Audit AES/HMAC keys and rotation-aware Share HMAC keys are never
  represented in config output; raw access logging is disabled because Share
  tokens are URL path credentials.
- Added strict short-lived Ed25519 JWT verification for Control Plane clients
  and admins: canonical bounded segments, duplicate-member rejection, exact
  issuer/audience/use/lifetime checks and bounded principal/role projection.
  Release manifests retain a separate Ed25519 trust ring.
- ASGI lifespan now exposes dependency-backed liveness/readiness only for a
  production lifecycle. Draining rejects new HTTP work, closes new/active WSS
  update sockets with restart semantics, stops the durable signal poller,
  closes S3 and releases the process lock in order. Online backups use ULID/
  SHA-256/canonical receipts, bounded retention and retry; backup failure removes
  readiness.
- Focused new production composition suite: `13 passed`, including a real
  second-process lock probe. Broad Control Plane, Cloud Audit/Share, WSS,
  schema-authority and hint regression adjacency: `153 passed`.
- Environment evidence still required: a real credentialed S3 bucket outage/
  latency/permission run. A multi-replica Control Plane additionally needs a
  first-party PostgreSQL schema/repository provider and HA/partition/load proof;
  neither is inferred from the deterministic S3 double or SQLite tests.

## 2026-07-11 - Protected Candidate build, signing and publication chain

- Added a dispatch-only Candidate workflow with channel concurrency locks,
  protected canary/stable Environments, minimum job permissions and explicit
  `publish_assets`/promotion inputs. The default remains a non-publishing
  canary Candidate, dry-run promotion and 1% rollout.
- Added a separate protected platform-stage workflow for Windows x64, macOS
  arm64 and macOS x64. A SHA-256-pinned external stager must produce the real
  Runtime plus browser/image/sandbox Pack trees and platform gate evidence.
  Missing binaries and placeholder trees fail before any signing and leave a
  typed failure receipt. Every tree also requires a supply-chain evidence
  digest, and its receipt records the pinned stager executable/adapter digest,
  so opaque staged dependencies cannot inherit the source-only scan.
- Added strict stage and recipe contracts. Candidate admission recomputes every
  file digest, rejects links/reparse points and target duplication, requires 12
  exact stages and pins each receipt to the same non-PR workflow-dispatch run,
  repository and commit.
- Added `DigestPinnedExternalSigner`: canonical ReleaseBuilder payloads go only
  over stdin; executable/adapter SHA-256 is checked before and after each call;
  stderr is discarded; stdout is bounded to a Base64 Ed25519 signature; the
  protected public key verifies the result before it can enter a manifest.
- Reused ReleaseBuilder for three product Cores, nine Capability Packs, signed
  Pack sidecars, Web manifest, SBOM and release metadata. Reused the existing
  source-order publication coordinator for domestic mirror, GitHub and CDN.
- Closed cross-channel and repeated-canary collisions: source roots/scoping
  participate in `build_digest`, mirror/CDN append `release_id`, stable keeps
  `v1.0.0`, and each canary tag carries its 24-hex build prefix.
- Added immutable build/signature/supply-chain/gate receipts and an evidence
  assembler compatible with the Control Plane's exact 17-gate contract.
- Focused Candidate suite: `11 passed`; targeted compile and Ruff passed; local
  license/secret preflight passed. Real protected runners, KMS/HSM signing,
  production stage binaries and three-origin publication remain external
  evidence and are not claimed by the fixture suite.

## 2026-07-11 - Versioned general HITL contract and restart continuation

- Replaced arbitrary interaction response JSON with a Runtime-owned v1
  `InteractionContract`: bounded text/textarea/select/checkbox fields, exact
  declared options, required/length rules, typed actions and presentation
  intent. Permission, information, connector login, conflict resolution and
  Artifact review now share this one durable contract.
- Connector login contracts have no input fields. They expose only a public
  connector state and the backend-declared begin-login/check-status/cancel
  actions. Password/token/secret-named fields and credential-shaped response
  values are rejected before an Event or response row can be committed.
- The Runtime core schema now persists the immutable contract and the accepted
  response's client request ID/fingerprint. A partial unique index and
  transaction-level fingerprint check make an identical retry replayable while
  rejecting reuse of the same ID for another interaction or payload.
- `interaction.requested` and `interaction.resolved` carry complete typed
  facts. Replay validates the contract, response and correlation ID. Restart
  projections therefore reconstruct the same pending form and accepted answer.
- Agent Turn Worker honors a tool follow-up only when the reviewed
  `ToolSpec.output_schema` explicitly declares `_ecorex_interaction`. The tool
  completes once, the Job checkpoints in `waiting_tool_followup`, and a
  restarted worker returns the validated human response to the model without
  repeating the side effect.
- React `InteractionStack` renders backend fields/actions with labels,
  keyboard/focus support, live validation/server errors and a busy state.
  Actions are frameless at rest and use the shared subtle hover/focus frame.
  The session retains one request ID and payload until Runtime acknowledgement.
- Verification: focused Python Runtime/Worker/Replay adjacency `60 passed`;
  schema authority and signed-migration adjacency `19 passed, 1 skipped`; Web
  typecheck passed and the current Web contract/state/accessibility suite
  `126 passed`.
  At this batch's final verification point the full Web build reached Vite
  output but the shared initial-JS gate remained `475.33 KiB > 475 KiB`; this
  batch therefore does not report the aggregate bundle gate as green.

## 2026-07-11 - Trusted, non-exclusive image intent routing

- Removed every concrete image term, route identity and tool-ID branch from
  `CapabilityPlanner`. The Planner now performs only catalog matching,
  availability, governance, versioned routing-policy evaluation, exposure and
  stable ranking. A source contract prevents `imagegen`, `media.image` or the
  retired `_IMAGE_INTENT` pattern from returning to that module.
- Added the Core-owned `IntentRoutingPolicy` contract and v1 built-in create/
  edit rules. Rules require `GENERATE_MEDIA` plus reviewed
  `media.image.create` / `media.image.edit` facets. Phrase/rule/facet counts and
  lengths and score boosts are bounded; repeated phrases/rules cannot stack
  priority. The current policy is built into Core. The injection seam accepts
  only an already trusted product policy; no MCP wire or extension contract can
  submit routing facets or a boost.
- Added the reviewed facets to the built-in media capability. A replacement
  capability with a different ID and the same reviewed contract receives the
  same route treatment. Free-form MCP names, descriptions, effects and intent
  tags remain search evidence only and cannot self-promote.
- Strong Chinese/English creation and reference-image edit requests promote the
  media capability to direct without removing read/fetch/vision/CDP/shell.
  Exact eligible tool selection remains stronger than a route hint (`use shell
  to generate image` keeps shell first). `image2`/`gpt-image-2` alone is not an
  action: model feature/price/availability/fault questions stay unpromoted, but
  an alias accompanied by generation/edit language routes normally.
- Product discussions such as “optimize image-generation intent routing”,
  architecture, pricing/model selection and retouch-workflow design are also
  suppression evidence rather than media actions.
- Added explicit suppression for analysis-only, negated and diagnostic intent.
  A media name in “image generation failed; only analyze” is recorded as an
  explicit reference but does not become an invocation. Mixed requests remain
  compositional: “do not generate; only edit this reference” suppresses create
  while retaining edit.
- Extended immutable decisions with bounded `matched_evidence` and
  `suppression_reasons`; the Plan now binds routing-policy ID, version and
  SHA-256 digest. Durable snapshot restart/Replay round-trips score, evidence,
  suppression and policy identity. Missing Pack, offline, disabled and
  administrator-denied candidates retain trace evidence but stay hidden and
  uncallable.
- Verification: focused Planner/snapshot/real Runtime composition suite
  `40 passed`; focused Planner/snapshot/Runtime/Gateway/Replay adjacency
  `58 passed`; MCP/Pack/Gateway/Worker/Replay/permission adjacency
  `132 passed, 2 environment skips, 1 unrelated stale handler-set expectation
  deliberately deselected and reported to the root integration owner`.
  `run-v1-lint`, 17-fragment Runtime schema authority, seven-authority server
  schema gate, Planner concrete-media scan and owned-file whitespace scan all
  passed. Repository-wide `git diff --check` was clean apart from existing
  line-ending conversion warnings.
- Known boundary: this deterministic policy improves direct exposure; an
  unlisted novel expression still sees the media tool through deferred
  discovery instead of invoking an unreviewed fuzzy classifier. Vocabulary or
  policy changes require a version/digest change. Rollback removes the policy
  promotion and returns all tools to catalog default exposure without deleting
  any capability or changing governance.

## 2026-07-11 - Real platform Packs and atomic Core+Pack activation

- Added repository-owned browser/image/sandbox Pack sources, a bounded child
  protocol, Playwright/Chromium nested runtime verification, managed-image
  provider refusal and fixed-shell sandbox contract acknowledgement.
- Added a content-bound `pack-python.json` contract and removed every Product
  fallback to PATH/system Python. Core staging now builds a complete Python
  closure and runs it before producing dependency evidence.
- Added deterministic Windows/macOS launchers, a Windows AppContainer + Job
  Object helper, Seatbelt/AppContainer behavior probes and the source-pinned
  platform stager for all three targets. Windows Product composition now
  injects the helper from the active verified slot; helper identity is checked
  again before each launch.
- Implemented ADR-075: the exact browser/image/sandbox archive+sidecar set is
  downloaded with per-file resumable source failover, verified at the outer
  release and inner Pack layers, staged under the Core payload and activated as
  one slot. Added composite receipts, Bootstrap/provisional/prior-slot
  revalidation, fail-closed content-verifier injection and cleanup of a slot
  renamed immediately before a failed composite recheck.
- Closed a composite-receipt trust gap: verification now recomputes the Core
  sub-tree with the fixed Pack projection excluded and compares it directly to
  the retained signed Core archive. Rewriting both a Core file and the mutable
  local `payload_digest` no longer reauthorizes altered Runtime bytes.
- Exercised the real local Playwright/Chromium closure. It exposed both valid
  zero-byte distribution members (now supported only for dependency files) and
  a 208.21 MiB nested browser runtime. Artifact limits are now identity-aware:
  Core remains 150 MiB, Bootstrap 10 MiB, Pack archive 500 MiB and Pack sidecar
  1 MiB.
- Platform staging now consumes only the `platform-stage` profile selected by
  `requirements/locks/manifest.json`; Core dependency evidence requires the
  complete locked inventory, while the nested browser runtime is a validated
  subset. Every dependency/supply-chain gate records both manifest and profile
  lock digests instead of trusting the runner's installed package list.
- Removed the `capabilities -> update -> capabilities` cycle through dependency
  inversion. `PackContentVerifier` lives in the update domain; the concrete
  Capability manifest adapter lives in product integration and is injected by
  Bootstrap CLI and server/update composition. Both import orders were
  executed successfully.
- Verification: final atomic update/Bootstrap suite `79 passed, 3 skipped`;
  Pack/platform/Candidate/manifest/builder suite `74 passed, 3 skipped`;
  Product Runtime/update composition suite `41 passed, 2 skipped`.
  `run-v1-lint`, `py_compile` and both Capability-first/Update-first import
  orders passed. The skips are environment/platform-specific native behavior
  tests.
- Evidence boundary: this Windows development machine has no MSVC `cl/link` or
  macOS clang/Seatbelt environment. Native source and protocol tests are green,
  but no local native-build receipt is claimed. GA still requires all three
  protected runner receipts plus real first-install/update/rollback evidence.

## 2026-07-11 - Adversarial routing and reserved extension names

- Replaced the narrow create/edit phrase list with versioned action-group ×
  deliverable-group evidence. Common poster, cover, illustration, drawing,
  cut-out and background-edit language now routes without embedding a concrete
  tool ID. Product discussion, pricing/model/fault questions and explicit
  negation remain suppression evidence.
- Split create-failure and edit-failure suppression. A user can now say
  “generation failed, edit this image instead” or “retouch failed, generate a
  new image” without the failed route suppressing the explicit fallback.
- Added Unicode NFKC/zero-width handling, invalid-surrogate fail-closed behavior,
  a 64 KiB routing budget, bounded 128-entry decision evidence and stable
  candidate ordering independent of registry insertion order.
- Core tool IDs and aliases are a reserved explicit-reference namespace. A
  Skill/MCP contribution named like `imagegen`, `shell` or another Core alias
  cannot cause the generic extension executor to receive the same explicit
  boost. The snapshot records `reserved-skill-reference:*` for diagnostics;
  the unique extension ID remains selectable.
- Runtime validation errors now project bounded safe locations/messages without
  reflecting invalid user input, including malformed Unicode that previously
  could make JSON error serialization fail a second time.
- Focused adversarial verification: `85 passed, 1 skipped`; the final full v1
  suite below includes the same cases.

## 2026-07-12 - Effect-routed ImageGen model-request integration

- Reconfirmed the image route as a backend-owned, versioned effect/facet rule,
  not an `imagegen` branch in the generic planner. The selected implementation
  can be replaced by any reviewed catalog entry implementing the same
  `media.image.* + generate_media` contract; the planner source gate rejects a
  concrete media tool or route identity.
- Added a full Agent worker regression that freezes the capability snapshot,
  builds the managed Gateway request and proves the image implementation is
  the first direct tool for a generation request while read remains direct and
  fetch, vision, CDP and shell remain deferred/discoverable. No route invokes a
  tool automatically; runtime availability, policy and invocation validation
  remain independent gates.
- Replaced global meta-word suppression with bounded local-context and ordered
  clause evidence. `生成图片说明`, `修复图片生成按钮`, `优化改图功能` and chart/
  caption/link requests no longer false-route, while compound deliverables such
  as `生成图片并写图片说明` remain valid. The latest explicit clause wins, so a
  retry after `生图失败` routes again and a final `只分析` cancels promotion.
- Focused planner/snapshot/invocation/model/worker/Runtime verification passed
  `109 passed`; a separate managed Responses adapter regression proves direct
  tool order survives the final provider payload projection. Ruff passed.

## 2026-07-11 - Reproducible dependency closure

- Added repository-owned hash locks for bootstrap, Runtime, development,
  cloud and platform-stage profiles. Python is fixed at 3.11.9, Node at
  22.23.1, and every external GitHub Action is pinned to a 40-character commit.
- CI/Candidate/platform jobs now use only the strict profile installer and
  `npm ci`; naked pip/npm installation, URL/VCS/path dependencies, lock drift,
  missing hashes and floating toolchains fail the dependency gate.
- Candidate identity, release metadata, receipts and SBOM bind lock manifest
  `c452d89bf9215c89c00638bc7bf39a0eed89a29fd3a63a5917c5abf3d691fa85`.
  Platform staging compares its installed inventory with the selected lock.
- Fresh Windows Runtime/cloud installation and import passed. Public binary
  availability probes found 21/21 Runtime packages for Windows x64, macOS 11+
  arm64 and macOS 11+ x64. Actual macOS installation remains a protected-runner
  gate, not a local claim.

## 2026-07-11 - Final browser, bundle and source-tree closure

- A real browser exposed that the post-Vite content-addressing pass could let
  generic and asset-specific string matches overlap on one quote, producing an
  invalid `import(""./chunk.js"")`. The rewriter now gives exact local assets
  precedence and rejects any overlapping generic token. The final bundle gate
  additionally invokes the JavaScript parser on every emitted chunk, so future
  rewrite corruption is blocked generically.
- Applied the same reference-authority rule to ReleaseBuilder reachability.
  This fixed a real `clientOperationOutbox` false orphan without weakening the
  rejection of stale or missing assets.
- Narrow mobile Artifact layout now reserves a real 44×44 px More target even
  when a desktop pointer emulates a 390 px viewport; feedback/retouch remain in
  the touch menu. Image preview opens in fit/contain mode with zoom retained.
- Browser Pack now validates every HTTP subrequest, not only the main URL,
  blocks WebSockets for the bounded office-browser contract and validates the
  nested Chromium manifest against duplicate/escaping paths. Explicit private,
  loopback and link-local subresources from public or `data:` documents fail
  before dispatch.
- Final local browser evidence: 1440×900, 1024×768, 768×900 and 390×844 in
  light/dark passed 8/8 with zero horizontal overflow and zero axe violations;
  a clean direct production page logged zero error/warn.
- Final frozen source-tree verification: `1208 passed, 14 skipped, 0 failed` in
  415.32 s; TypeScript and `138/138` Web tests passed; 17 content-addressed Web
  assets passed syntax and size gates; Ruff, 17 Runtime/7 server schema
  authorities, strict legacy/public/design/dependency/supply-chain/
  reproducibility gates, npm audit and whitespace checks passed.
- The final local Windows Ed25519 candidate drill completed in 352.7 s with
  background download, user-confirmed activation, bootstrap 200, refresh-safe
  caching and signed fault rollback. Its report is explicitly Core-only because
  the local host lacks a trusted native compiler. Formal Core+three-Pack
  Windows/macOS evidence remains bound to the protected platform workflow.

## 2026-07-12 - Automatic public Bootstrap freshness renewal

- Split immutable release authority from renewable pointer freshness. The
  Control Plane accepts only same-sequence, same-revision, same-target renewal
  signed by the independent online publication key.
- Added a durable startup catch-up + periodic refresher with an eight-hour lead,
  one-hour check and ten-minute database lease by default. Attempts,
  preparations, events, state and alert outbox rows survive restart; exact
  prepared bytes and deterministic publication request IDs prevent duplicate
  KMS identities or external writes.
- Reused the existing stage → object CAS → credential-free HTTPS readback →
  canonical activation saga. Added recovery for a process exit after exact
  activation/readback but before refresh-success bookkeeping, plus phase lease
  renewal and fail-closed readiness after expiry.
- Production composition now supports a digest-pinned workload-identity
  KMS/HSM signer. Missing signer is explicit `unconfigured`; signer, object or
  readback failures retain the previous database-active pointer and emit safe
  audit/outbox/health evidence. No path emits a rollout/update signal.
- Added administrator status and one-shot refresh API/client/CLI operations;
  the supplied client request ID is durably replayed through Control Plane
  idempotency.
- Verification: server schema authority passed; the combined pointer/index,
  schema, release-flow, admin and production suite passed `73 passed`. The
  refreshed saga suite passed `18 passed`, and production composition passed
  `14 passed`. No live KMS, public HTTPS object store or rollout was invoked by
  this local deterministic evidence.

## 2026-07-12 - Post-QA release/freshness hardening

- Closed three P1 findings: freshness readiness now binds automation enablement,
  signer, live task, heartbeat and scheduler error; release/publication roles
  reject identical raw Ed25519 keys under different aliases; external signer
  stdout/stderr are bounded streams and timeout/overflow terminates and reaps
  the complete process tree without exposing stderr.
- Closed scheduler timing gaps: check interval cannot exceed half the lead
  window, lead plus clock skew must remain below the 24-hour TTL, and check +
  lease + signer timeout + skew must fit before the renewal boundary.
- Promotion journal schema v3 binds the complete semantic rollout target
  (channel, percentage, sorted organizations/accounts and minimum compatible
  version) into prepare/final evidence. Parameter drift fails before any server
  call. Manual freshness CLI accepts an explicit request ID or uses a locked,
  fsynced pending journal bound to endpoint + immutable authority digest. It
  survives ambiguous response loss, clears only after observed success and
  audits invalidation when the release target changes.
- Verification: focused QA suite passed `58 passed`; the final release/security/
  schema/admin/production adjacency suite passed `106 passed`. Ruff,
  `py_compile` and the eight-authority server schema gate passed. No live KMS,
  public CDN/object store or rollout mutation was used.
- Final P2 regression: simulated server success followed by lost client
  response changed the active expiry, yet a new CLI process reused the pending
  ID and the server performed one publication only. After observed success the
  next invocation produced a new ID. CLI/client/freshness focused tests passed
  `30 passed`.

## 2026-07-12 - Release-gate execution integrity

- Removed `e2e` and `migration-dry-run` from the quality writer's automatic
  passed-gate declaration. Quality evidence now hashes the real browser E2E and
  complete migration execution logs; E2E is promoted to a gate only after it
  is bound to the signed Candidate and verified Windows platform-stage set.
- Candidate quality now fetches and verifies the full object graph for fixed
  v0.3.0 commit `f0750d247bfe52ffb95c137cadc9983a03010690`.
  The migration gate explicitly runs copy-on-write, Product coordinator,
  quarantine, released-schema, signed Candidate migration and activation
  rollback suites plus both schema-authority checks.
- Added required `image-shared-storage` and `image-soak` Control Plane gates.
  The bounded job creates isolated digest-pinned PostgreSQL 16.9 and MinIO and
  runs 256 jobs/48 workers across exactly two node IDs. A separate protected
  runner repeats that real test for at least four hours. No skip-to-pass path
  exists; absent protected capacity keeps the Candidate blocked.
- Added release-bound evidence that joins execution, commit/run identity,
  Candidate receipt, release/build identity and the complete Windows
  Core/Bootstrap/browser/image/sandbox stage boundary before the three runtime
  gate receipts are written.
- Added filesystem-authoritative source checks to Candidate and CI, closing the
  untracked-file blind spot in `git diff --check`. The supported lint/compile
  surface now includes all current-v1 scripts, platform stager and Capability
  Pack Python without pulling historical scripts back into production gates.
- Focused integrity, workflow, lint, quality and Candidate tests passed `12`;
  Ruff passed for all touched Python files. The protected four-hour soak and
  signed platform jobs remain intentionally unclaimed until their runners
  execute them.

## 2026-07-12 - Signed and typed release-evidence hardening

- Upgraded the successful Candidate build receipt to schema v2 and signed its
  canonical, domain-separated unsigned body with the same digest-pinned
  external Ed25519 signer used for artifacts and the release manifest. The
  receipt authenticates commit/staging provenance, exact 15-stage set, Web
  tree, dependency lock and the complete manifest artifact projection.
- Replaced log-string evidence with full pytest and migration JUnit plus
  Playwright JSON. Validation requires at least 1,000 executed full-suite cases
  and critical Runtime sentinels, the exact migration corpus with zero skips,
  and exactly 11/11 browser cases with zero skips.
- The image shared-storage runner now parses per-round JUnit and requires the
  two fixed pytest node IDs and exactly two passing, non-skipped test cases;
  stdout text can no longer mint a passing gate.
- Release binding verifies the real manifest and Candidate receipt signatures
  with the protected public key, then checks complete manifest/Candidate/
  staging/run identity. Migration is rebound after Candidate creation. Stage
  producer receipts bind `workflow_run_attempt` to staging provenance so
  rerun artifacts cannot be mixed.
- Gate receipt schema v2 carries release ID, version, channel, build digest and
  exact manifest SHA-256. The assembler rejects mixed runs and validates the
  complete publication receipt against the manifest.
- Added stable evidence I/O that rejects symlink/reparse components, detects
  opened-file identity/size/mtime drift, refuses NaN/Infinity JSON, and creates
  immutable receipts using exclusive create plus fsync.

Verification:

- Signed Candidate, typed gate, publication, process-boundary, stable-I/O and
  reproducibility suite: `48 passed, 1 skipped`; the skip is the real Windows
  symlink case where this host lacks symlink privilege.
- `python scripts/run-v1-lint.py --compile`: passed.
- Candidate/platform workflow YAML parsing: passed.
- A real Playwright 1.61.1 / Node 22 JSON report from 11/11 GA cases was parsed
  successfully by the new quality validator.

## 2026-07-12 - Cross-runner reproducibility becomes a Candidate gate

- Added required `ci_run_id` and `ci_run_attempt` Candidate inputs and a
  read-only provenance job. It reads the same-repository run API record and
  downloads only the four named Ubuntu/Windows/macOS byte-contract artifacts
  from that run; the existing typed verifier rejects stale, PR/fork, wrong
  commit/run/attempt, incomplete, linked or non-identical inputs.
- The signing job now consumes that immutable source evidence and invokes the
  dedicated reproducibility binder only after the schema-v2 Candidate receipt
  and signed manifest exist. The binder requires its canonical Web tree to
  equal the Candidate receipt's signed Web tree.
- Added `reproducibility` to the Control Plane required gate set. The typed gate
  writer has a dedicated strict validator for
  `ecorex-release-bound-reproducibility`; raw comparison evidence cannot mint a
  receipt. Source evidence, the release-bound projection and its receipt are
  retained in the Candidate artifact and enforced by the release assembler.
- Focused CI provenance, release-integrity and Candidate pipeline tests passed
  `38 passed`; workflow YAML parsing and touched-file Ruff checks passed.

### Attempt-bound artifact hardening

- Closed the GitHub rerun ambiguity: Candidate now fetches the run artifact
  metadata and the attempt-specific run API record first, requires exactly
  four expected non-expired artifacts whose
  workflow/repository/head identity and timestamps belong to the selected
  attempt, then downloads those immutable Artifact IDs rather than a name
  pattern. Source and bound schema v2 retain artifact metadata SHA-256 plus
  every artifact ID, archive digest, size and creation/update time separately
  from the downloaded byte-contract digests.
- Accepted GitHub's documented workflow-run path shape only as either the exact
  base workflow path or that path suffixed with `@main`; evidence canonicalizes
  the base path and negative tests reject `@feature` and all other paths.
- Kept the artifact set exact-four by design. Full reruns/new runs are
  supported; partial job reruns that retain another attempt's runner artifact
  fail the timestamp/set check and require an operator full rerun, preventing a
  mixed-attempt reproducibility receipt.
- Final focused verification: reproducibility/release/dependency/Candidate
  suites `45 passed`; Control Plane release/admin/WSS adjacency `56 passed`;
  dependency locks, v1 lint/compile and all three workflow YAML parses passed.

## 2026-07-12 - Gate-writer authority reauthentication audit

- Every gate-writer invocation now supplies the signed Candidate receipt,
  trusted release public key, signed manifest, exact staging provenance and
  expected staging run. Generic execution/platform and reproducibility gates
  additionally supply their raw typed source evidence.
- The writer snapshots bound evidence, raw source, Candidate, manifest and
  staging authority exactly once with stable-file/link/TOCTOU checks before
  independently authenticating and recomputing the submitted bound bytes.
  This removes cross-read path replacement windows.
- Generic Candidate authentication now requires the exact canonical receipt
  bytes emitted by the production builder, in addition to Ed25519 signature,
  manifest and staging checks. A whitespace-reformatted receipt is rejected.
- Generic gate calls are restricted to one execution gate or the exact paired
  Windows/macOS platform set. Reproducibility remains an isolated singleton;
  ambiguous mixed gate sets cannot create receipts. Non-bound quality and
  supply-chain gates retain their existing typed validators but still require
  the authenticated Candidate authority.
- Independent focused verification: `41 passed`; isolated-cache v1
  lint/compile and all three workflow YAML parses passed.

## 2026-07-12 - Managed chat policy upgraded to GPT-5.6 SOL

- Kept the migration-stable local model ID `ecorex-chat`, while moving its
  authoritative upstream identity to `gpt-5.6-sol`. The model directory now
  publishes the real display name, aliases, `chat/tools/vision/reasoning`
  capabilities and a versioned execution policy instead of making WebUI infer
  any of those facts.
- Added one shared v1 policy authority consumed by Runtime and Model Gateway:
  policy `ecorex-chat-gpt-5.6-sol@1.0.0`, fixed `medium` reasoning and an exact
  272000-token compaction threshold. Production startup rejects any environment
  mapping other than `{"ecorex-chat":"gpt-5.6-sol"}`; it never silently routes
  the stable local identity to another upstream.
- The Runtime freezes the policy in the model catalog snapshot, sends it in
  every `ModelGatewayRequest`, and records it in the durable `model.requested`
  event. Gateway rechecks the complete policy before provider projection.
- Every upstream Responses request now carries both
  `reasoning={"effort":"medium"}` and the actual server-side trigger
  `context_management=[{"type":"compaction","compact_threshold":272000}]`.
  The existing provider bearer-token logical name, environment variable and
  read path were not changed.
- The thin WebUI contract maps this backend policy, validates it at Bootstrap,
  and continues to select the canonical local ID; it does not choose upstream
  model, effort or compaction settings.

Verification and evidence boundary:

- Focused capability/catalog/Runtime/worker/Gateway suite: `219 passed`.
- Additional managed Gateway/server/schema/supervisor/import suite:
  `57 passed, 1 skipped`.
- Policy-focused catalog/composition/provider/worker suite: `66 passed`.
- Generated Runtime contract check, WebUI TypeScript typecheck and 37 focused
  Web contract/model-selection tests passed; the full v1 lint/compile gate passed.
- MockTransport evidence proves the exact provider payload and accepts an
  emitted opaque compaction item, but no live >272000-token provider run was
  performed here. Therefore this batch claims a real configured trigger and
  durable policy contract, not evidence that a production compaction event has
  already occurred. Live `gpt-5.6-sol` entitlement, long-context compaction and
  quality/latency soak remain deployment gates.

## 2026-07-12 - Permission-safe durable tool admission

- Added one append-only `InvocationAdmission` permit before every Worker tool
  dispatch. It binds Job/Thread/Turn/execution batch, exact tool version,
  canonical argument digest, idempotency key, frozen permission snapshot,
  current permission snapshot and verified ledger-chain digest, current
  availability digest, approval interaction, effective sandbox and admission
  time. Capability dispatch resolves this durable permit; a caller boolean is
  not authority.
- Permission mutation and in-process admission share the same product lock.
  More importantly, the admission `BEGIN IMMEDIATE` transaction rechecks the
  current `runtime_permission_state` against its append-only ledger before the
  permit INSERT. A separately locked Runtime process that revokes first makes
  the stale admission retry current governance; an admission that commits first
  is auditable as already started under that permission fact.
- Current authority is a non-broadening intersection: a new default profile,
  administrator deny, missing/quarantined Pack, disconnected Connector or
  offline network can tighten an old Turn; a later full-access profile cannot
  relax the Turn's frozen requirements.
- A `started` ToolExecution without a permit is restart-safe and resumes the
  approval/admission path. Only a permitted non-idempotent execution can enter
  the uncertain-human-resolution path. Resolved permission approval is checked
  for exact `allow` and is bound back to the Job checkpoint, model tool-call ID,
  arguments and execution batch; a resolved deny cannot mint a permit.
- Tool arguments are schema-validated and canonicalized before Tool Item or
  HITL creation. Invalid opaque/non-idempotent commands therefore fail without
  approval UI, an execution record or an uncertainty warning.

Schema/release impact:

- This pre-GA change extends the compiled `tool-executions` schema fragment
  with `invocation_admissions` and append-only triggers, so the compiled product
  schema digest and signed Candidate target-schema digest change. Runtime still
  never repairs an existing database at startup. Any database belonging to a
  previously signed v1 Candidate must advance through a signed declarative
  storage migration and Candidate dry-run/live receipt; only disposable
  unsigned development databases may be recreated. The v0.3 import remains a
  copy-on-write import into the current compiled v1 schema.

Focused evidence: admission/permission/Worker/schema/shell tests cover
full-access-to-default queued and restart recovery, current admin deny,
same-process and separately locked revocation races, cross-batch permit replay,
deny-ID forgery, invalid arguments, pre-admission recovery and post-admission
non-idempotent uncertainty.

## 2026-07-12 - Exact, batch-scoped Skill resource grants

- Replaced model-facing Skill name/alias reads with the immutable
  `skill:<extension_id>@<revision_id>` discovery contract. `skill_search`
  returns schema version, batch-frozen Extension snapshot, contribution
  snapshot and bounded name/description/tag metadata; it exposes no host path,
  CAS digest or source filename.
- A completed `skill_search` now discloses only the generic `skill_read`
  endpoint. Before returning content, Runtime requires that exact Skill ID to
  exist in a completed search under the same Job, Thread, Turn, execution
  batch, capability snapshot, permission snapshot and Extension snapshot. It
  recomputes the full frozen search result and independently recomputes its
  canonical SHA-256 before binding the search ToolExecution ID and digest into
  the read outcome.
- Explicit Skill mentions now affect search order only. They no longer promote
  `skill_read`. A Skill grant never enters Tool/MCP/Connector disclosure, and
  reference reads remain restricted to immutable IDs from that exact Skill
  revision's frozen inventory.
- Worker model projection reconstructs the generic endpoint disclosure from
  completed SQLite facts. Exact content authority is reconstructed separately,
  so Runtime restart retains the link while cross-Skill, cross-reference,
  cross-batch, guessed/alias, stale-revision and forged-result attempts fail
  closed.

This changes the Core Tool catalog contract and catalog snapshot digest but
does not add or alter a storage-schema object. Existing signed Candidates still
require normal catalog/protocol compatibility gating; no startup schema repair
or legacy compatibility shortcut was introduced.

## 2026-07-12 - Batch-scoped Tool Search and exact Describe grants

- Added immutable `execution_batch_id` to every `ToolExecution` identity,
  repository record, query, Worker begin path, index and identity trigger. A
  ToolExecution can be created only when its Job, Turn and execution batch
  agree; an invocation admission must now match the same batch stored by the
  execution itself.
- Closed the model-facing deferred-tool shortcut. `tool_describe` accepts only
  the exact `tool:<tool_id>@<tool_version>` discovery ID returned by a completed
  `tool_search` under the same Job/Thread/Turn/batch/capability/permission
  scope. Bare canonical names, aliases, guessed IDs and stale versions produce
  a structured non-grant result. Runtime-internal `CapabilityService`
  description by canonical tool ID remains available for approval and UI code.
- The Describe result binds `search_tool_call_id`, exact `discovery_id` and the
  canonical search-result SHA-256. Before issuing it, Runtime recomputes the
  bounded search against the batch-frozen model catalog and requires the whole
  recorded result to match. Durable grant reconstruction independently joins
  both completed execution facts to the same batch and verifies the exact
  deferred Tool decision, so restart does not depend on process memory.
- Worker model projection and pre-Item/pre-HITL invocation checks now query
  disclosures by execution batch. A grant from an earlier steer batch cannot
  enter a later model request or authorize its invocation.

Focused evidence: Tool disclosure, Worker, admission and execution-schema
suite `42 passed`; it covers real Search -> exact Describe -> invocation,
restart reconstruction, missing/forged scope, bare name, alias, stale version,
forged search result, malformed Describe result and cross-batch replay.

## 2026-07-12 - Bounded model-visible Tool working set

- Added the versioned `tool-projection-budget@1.0.0` contract. A model round can
  receive at most 16 complete descriptors, no more than 12 of which may be
  durable deferred grants. Each canonical UTF-8 descriptor is capped at 96 KiB
  and the canonical descriptor batch at 256 KiB. The deferred catalog remains
  bounded separately at 1,024 identities and does not send schemas.
- Runtime now creates one deterministic, execution-batch-bound Tool projection
  in frozen plan score order. Frozen direct tools are projected first and are
  never displaced by an extension grant. An oversized direct set fails with a
  typed, non-retryable outcome before provider I/O; an over-budget deferred
  grant remains searchable/deferred but has no schema or invocation authority
  in that round.
- Initial authorization and the immediate pre-dispatch check both rebuild the
  same bounded projection. A provider cannot call a grant suppressed from the
  request, and Runtime restart reconstructs the same projection from durable
  Search/Describe facts.
- `ModelGatewayRequest` validates count and canonical-byte limits. The fixed
  Responses provider independently repeats them so an unvalidated object copy
  or future alternate transport cannot bypass the network boundary.
- `model.requested` now records the budget version, canonical schema byte count,
  projected IDs and suppressed IDs. It records no descriptor schema.

Focused tests cover provider floods, exact 96 KiB and aggregate 256 KiB
boundaries, 16/12 count limits, Core-first ordering, restart determinism,
deferred suppression and rejection at both authorization and execution fences.

## 2026-07-12 - Share image rendition authority closes silent degradation

- Added one shared media-publication contract used by Local Runtime and the
  Control Plane. Schema-v2 image Artifacts without a bounded raster rendition
  now fail before a snapshot identity or public URL is committed; the old
  fallback that treated a primary source blob as a preview was removed.
- The durable worker repeats the contract immediately before external I/O and
  treats an already frozen invalid payload as terminal. The Control Plane
  repeats it before render/staging/linking and public resolution; media remains
  accessible only through the active snapshot token and declared link.
- Added stable user-safe errors for missing, over-16-MiB, unsupported, invalid
  and over-64-MiB previews plus schema-v1 issuance. WebUI maps the codes to
  plain Chinese while technical details retain only the stable code. No error
  contains an Artifact name, local path, digest or provider detail.
- The current Artifact service can attach immutable renditions but does not own
  a general safe resize pipeline. Sharing therefore refuses an image that has
  no rendition instead of decoding the original in the Control Plane. Existing
  schema-v1 snapshots remain byte-compatible/readable; only new issuance is v2.

## 2026-07-12 - Verified provider provenance and fair Tool Search

- Added immutable provider provenance to `ToolSpec`, capability decisions,
  plan snapshots, Tool Search summaries, exact Describe responses and MCP
  contribution snapshots. The provider record contains only kind, exact
  provider/revision, trust verdict, optional key ID and evidence SHA-256; raw
  detached signatures are not projected.
- The safe legacy `ToolSpec` default is reviewed Core, but the `mcp.*`
  namespace explicitly rejects it. MCP specs must use their verified
  `mcp.<extension_id>:` namespace, exact `extrev_*`, deferred exposure, zero
  product routing facets and zero priority bias. MCP trust never sets
  `product_reviewed`, including for a Core-bundled protocol transport.
- `MCPRuntimeBinding` now requires the non-serializable
  `VerifiedExtensionManifest`. Runtime re-verifies that exact candidate under
  current trust, matches its stored unsigned revision and exact append-only
  signature evidence, then derives sanitized Tool provenance. MCP list
  metadata has no path to this constructor.
- Discovery policy `ecorex.discovery@1.2.0` binds
  `ecorex.provider_fairness@1.0.0`. Exact-reference matches precede quotas;
  broader results reserve at most half of remaining slots for reviewed Core,
  then balance by exact provider provenance. This retains `limit=1` exactness,
  prevents a 256-tool provider flood and remains deterministic after restart.

This changes catalog/discovery/snapshot digests but adds no database object.
Previously emitted pre-GA capability snapshots without provider provenance
fail closed instead of being assigned a guessed source.

## 2026-07-12 - Image concurrency deadline, backoff and staged-result closure

- Closed a durable-capacity leak in both image Store adapters. Schedulable jobs
  whose deadline elapsed are now atomically and idempotently changed to
  `failed/deadline_exceeded` by submit, lease or explicit reclaim. PostgreSQL
  holds the scheduler control lock and row-locks candidates with
  `FOR UPDATE SKIP LOCKED`; SQLite serializes the equivalent transition with
  `BEGIN IMMEDIATE`. Exactly one terminal event is appended and the row stops
  consuming queue admission capacity.
- Added bounded RFC `Retry-After` handling to the managed image provider.
  Delta-seconds and HTTP-date are normalized to 1–3600 seconds; missing or
  malformed values fall back to exponential jitter. A submit-time 429 is a
  known non-acceptance and retries submit, while a result-download 429 retains
  recover-first authority because the provider effect is already known.
  Every 429 also opens the durable provider/model/operation/size scope until
  at least the maximum of the bounded hint, breaker cooldown policy and an
  existing fence. This is independent of the ordinary breaker failure
  threshold, so queued jobs cannot continue a rate-limit stampede.
- Replaced the read-only circuit check in the Worker path with a durable
  transactionally leased half-open decision. After cooldown, only one replica
  may call submit/recover for a provider/model/operation/size scope. Other
  replicas persist retry wait until the probe lease; success resets the
  breaker, failure reopens it, and a crashed probe becomes eligible only after
  the bounded lease expires.
- CAS put/describe/read now renew the Job lease. The exact result and usage are
  persisted together in the committing checkpoint. A crash or database fault
  after CAS staging therefore resumes by verifying CAS and completing the
  result/usage/event transaction without another provider call. Invalid staged
  identity fails closed; corrupt/missing staged bytes return to recover-first
  rather than being published.
- No image storage schema object changed. The half-open lease deliberately
  reuses the existing durable breaker `open_until`; deadline cleanup and staged
  commitments use existing state/checkpoint columns. Production composition
  derives the probe lease from two provider timeout windows plus the Job lease.

Focused deterministic evidence uses only a controlled fake provider: 32
concurrent expiry/restart contenders append one terminal event, 16 concurrent
Workers issue one half-open provider call, a slow CAS write crosses the original
lease while heartbeats keep ownership, and an injected final-commit fault
restarts with `submit=1`, `recover=0`, one usage record and one completion event.
This does not claim a real-provider or 24-hour production soak.

## 2026-07-12 - Hallmark WebUI interaction closure

- Kept the locked Workbench design intact: one clipped WorkspaceSurface,
  transparent idle button borders, ordinary surfaces without shadows,
  contain-first image preview, Artifact hover/focus rail and touch More sheet.
- Fixed the only product interaction defect found: the `<640px` rule removed
  the whole send-disposition control. The stacked Composer now retains
  steer/queue/replace, and a 320×568 touch test executes `排到下一轮` end to end.
- Updated the GA fixture to the authoritative `ecorex-chat` →
  `gpt-5.6-sol`, medium-reasoning, 272000-compaction policy and current event
  envelope. Added real-DOM tests for reasoning replacement/terminal archive,
  first-turn zero output, retry, persisted HITL and Chinese share copy.
- Extended the light/dark browser matrix with the 320px Hallmark floor and a
  rendered-line check for clickable labels. Shorter Composer copy kept the
  unchanged initial-JS budget below 475 KiB.

Evidence: Playwright 20/20, Web tests 154/154, TypeScript clean, strict design
gate clean, production build 474.99 KiB initial JavaScript. No Python backend,
physical device, screen reader or public share origin was certified here.

## 2026-07-12 - Connector-login HITL Web closure (provisional)

- Routed connector login begin/check/cancel through dedicated lifecycle
  endpoints. Connector cards never submit these actions through generic
  `/respond` and remain backend/SSE/projection authoritative.
- Added safe pre-opened OAuth windows, device-code verification UI, bounded
  polling and manual checks. `authorization_required` and
  `reauthorization_required` stop polling, clear stale URLs/codes and retain a
  clear retry-login action; unknown or mismatched responses fail closed.
- Pending checks no longer refresh the full connector catalog every two
  seconds. Connected/reauthorization facts refresh catalog authority, while
  successful completion refreshes the current projection and waits for the
  backend-bound new execution batch.
- Added GA browser scenarios for OAuth, device code, dedicated cancellation,
  partial scope and interrupted completion. All assert zero connector use of
  generic `/respond`.

Interim evidence: Playwright 25/25, focused Web tests 50/50, direct TypeScript
clean, design debt zero, content-addressed build 474.68 KiB initial JavaScript.
The authoritative Runtime schema was still changing in the backend batch, so
generated-contract/full Web gates are intentionally deferred until codegen is
frozen.

## 2026-07-12 - Connector progressive-disclosure and crash-fence closure

- Replaced model-facing action aliases with four governed Core tools:
  `connector_search`, `connector_describe`, `connector_read` and
  `connector_write`. Exact discovery IDs bind the instance, account, action,
  frozen Connector catalog digest and one durable execution batch.
- Added backend-owned intent aliases to Connector action contracts. Ranking can
  favor the intended office action without hardcoding Feishu/Tencent action
  families in the planner or hiding unrelated capabilities.
- Added append-only Connector-login generations and dedicated begin/check/cancel
  endpoints. Flow activation, callback consumption, credential activation or
  swap, interaction state and authority refresh use durable fenced facts;
  startup recovery is per-reference and emits sanitized deferred diagnostics.
- Added informed default-mode write approval. The prompt names the Connector,
  account and exact action without copying user content, and resume recomputes
  the same-batch Describe descriptor/digest before admission. Permission changes
  between projection and admission now enter this HITL path with the complete
  invocation context.
- Added durable operation/invocation/idempotency fencing, pre-dispatch current
  admin-policy sampling and supervised late-result handling. A timed-out write
  that later succeeds becomes a replayable completion with one provider call;
  unresolved writes remain explicitly reconcilable.
- Disconnect now has a per-instance revocation claim and stable provider
  idempotency key. Maintenance autonomously resumes abandoned `draining` and
  expired `revoking` states; provider revocation, credential cleanup and final
  deletion remain separately fenced.
- Registered the `connector-agent-runtime` schema fragment in the schema
  authority and regenerated the thin-Web Runtime contract.

Local focused evidence is 99 passing Connector/Runtime/approval tests, two
schema-authority gate tests, clean Ruff, and generated contract check. Provider
adapters are deterministic fakes; real Feishu/Tencent credentials, deployed
multi-process soak, and atomic oversized-result Artifact staging remain GA
gates rather than certified results.

## 2026-07-12 - Connector result Artifact publication closure

- Added `connectors-v6` result staging and exact invocation-envelope replay.
  Inline provider JSON is capped at 512 KiB; larger canonical JSON enters CAS
  as a secondary ready `data_export` deliverable. Artifact metadata,
  invocation/idempotency/outbox, deterministic completed Artifact Item and the
  real user-thread event linearize in one Runtime SQLite transaction.
- Model-originated reads and writes now share stable durable call identities.
  Same-key concurrency waits, restart and late-provider success finalize from
  local staging, and an already prepared stage blocks human reconciliation
  from reopening provider execution.
- Added secret-free `result_unavailable` receipts whose digest identifies only
  the bounded receipt, not rejected provider bytes. Recovery deferrals emit a
  redacted, deduplicated Connector outbox fact and maintenance retries them.
- Added the protected `artifact_read` Core handler with account/thread,
  visibility, family/role/MIME/status, exact Revision/SHA, strict UTF-8 and
  character-bound checks. It never exposes a filesystem path.
- The supported v0.3.0 copy-on-write target creates final `connectors-v6`
  schema directly. Unreleased v5 prototype databases fail closed and remain
  unchanged; Runtime performs no implicit DDL repair.

Evidence: focused Connector/Artifact/schema tests 102/102, expanded
Connector-or-Artifact tests 250 passed/1 skipped, Runtime schema-authority
gate passed, and targeted Ruff passed. External real-credential and deployed
multi-process soak remain release-gate work.

## 2026-07-12 - Critical recovery execution lane

- Replaced the shared HTTP read-only exception list with an independent,
  process-local `RecoveryExecutionGate`. Its only fixed scopes are managed
  session revocation and activation of an already staged local update; it does
  not inherit or override business Runtime health.
- `update.check` now remains a normal managed mutation. It requires the active
  managed session and a healthy Runtime permit, so Critical mode rejects it and
  logout cannot turn it into an unauthenticated network request.
- `update.activate` uses loopback Runtime bearer + Origin + CSRF as the local
  installer credential. It requires an exact `awaiting_user` transaction and
  re-verifies the stored signed manifest, artifact signature/digest, active
  metadata, install journal, staged slot, Capability Packs, platform/channel
  and slot security before activation. It never calls the cloud feed in this
  recovery path.
- Recovery async dispatch captures one permit, rechecks it after each await and
  before consuming results, and installs the same permit at every shared SQLite
  commit boundary. No gate lock crosses an await. Closing the recovery lane
  between `BEGIN` and `COMMIT` rolls back the request.
- Added whole-database table-diff tests. During Critical recovery, only
  `managed_session*` and `runtime_update*` tables may change; Thread, Turn,
  Item, Artifact and Connector authority remains byte-for-byte unchanged.

Focused evidence: recovery/update tests 14/14 and managed-session,
InstallCoordinator and Runtime Critical integration tests 42/42; targeted Ruff
and Python compilation passed.

## 2026-07-12 - Permission/Turn acceptance linearization

- Added one synchronous `RuntimeComposition.admit_turn` boundary. It captures
  the current permission first, generates immutable config/capability/policy
  snapshots, and persists `turn.accepted` before releasing the same mutation
  lock used by permission updates. Awaitable acceptance callbacks are rejected,
  so no provider call or await can retain the permission lock.
- Migrated create, queue, replace and live replay to this boundary. Precise
  retouch now holds the same lock only across snapshot capture and its local
  Artifact/Turn/Job product transaction; adapter execution and notification
  remain outside it.
- Added a second SQLite fence for multi-process races. Product Turn creation
  supplies the permission account, and the Kernel write transaction verifies
  the frozen permission snapshot against the current mutable row and its
  append-only ledger before writing `turn.accepted`.
- Fixed a discovered replace defect: converting `ReplaceTurnRequest` to the
  planning request included the extra `reason` field, so HTTP replace failed
  before it ever entered Turn preparation. The planner now receives only
  `CreateTurnRequest` fields and reconstructs the canonical replacement inside
  the shared admission.
- Deterministic concurrent tests pause after old permission capture, start a
  permission update, and prove the update cannot return 200 until the old Turn
  has committed. Every Turn accepted after that 200 uses the new
  `permission_snapshot_id`; a stale cross-process prepared context is rejected
  with no accepted Event.

Evidence: 61 focused permission/composition/create/queue/replace/live
replay/retouch/Kernel tests and 67 Worker/tool-admission/retouch-integration/
Runtime-hardening tests passed; targeted Ruff and compilation passed.

## 2026-07-12 - Runtime Critical-request atomic publication

- Replaced independently mutable requested error/time fields with one frozen,
  first-writer-wins Critical request published under a dedicated short lock.
  Concurrent timeouts can no longer splice one caller's error code with another
  caller's timestamp.
- A published request closes admission before the main gate lock is available.
  Exactly one caller owns inline/background completion; competing requests do
  not create additional closer threads or overwrite the diagnostic fact.
- Permit issuance from a previously healthy admission now also checks the
  closure-request flag, covering the interval between non-blocking publication
  and final epoch latching. No gate lock is retained across provider waits or
  SQLite transactions.

Evidence: Runtime gate/invariant tests 21/21, Worker/Connector/Device/Recovery
permit integration tests 30/30, and targeted Ruff passed.

## 2026-07-12 - Runtime commit, lease and permission consistency closure

- Added one composable request/Job commit guard to the shared SQLite
  connection. Direct commits, nested service guards and `executescript` now
  retain the outer Runtime authority; a closed epoch rolls the transaction
  back before bytes become durable. In-memory Job permits retire only from an
  after-commit callback, so a rolled-back terminal transition cannot orphan a
  live durable lease.
- Fenced Job permit publication by the current durable lease token. A delayed
  old lease generation can no longer delete or replace the permit belonging to
  a newer generation. Gate admission is a short validation step and never
  retains a Runtime lock while SQLite or a provider is active, eliminating the
  prior database/gate lock-order inversion.
- Linearized permission capture and Turn acceptance for create, queue,
  replace, Live Replay and precise retouch. The process lock covers only
  permission read, immutable snapshot creation and local acceptance commit;
  the Kernel repeats the permission-ledger check inside SQLite for
  cross-process races. Provider calls and awaits are outside the lock.
- Replaced permission audit SQL N+1 with an already-verified revision map.
  `verified_sample_scope` reuses one complete ledger/audit verification only
  inside one synchronous invocation-governance call, reducing its permission
  SELECTs from 12 to 4. The next invocation verifies afresh, and durable
  admission still rejects a cross-process revocation before provider dispatch.
- Made Critical requests first-writer-wins immutable facts. Error code and
  timestamp publish atomically, only one closer owns epoch completion, and a
  closure request immediately blocks permits issued from an earlier admission.

Deterministic evidence includes old/new lease generation barriers, rollback
after scheduled permit retirement, create/queue/replace/replay/retouch
permission races, cross-process revocation with zero provider calls and
concurrent Critical requests. The final complete Python gate is recorded in
the verification ledger.

## 2026-07-12 - Durable event delivery and bounded Connector shutdown

- Promoted Artifact event delivery to a lifecycle supervisor. Claim, actual
  sync-thread/async-provider dispatch, result handling and acknowledgement all
  revalidate the Runtime/local epoch. Provider calls have a hard timeout and
  token/digest-fenced lease heartbeat; a timeout leaves the immutable event
  pending and immediately recoverable instead of duplicating dispatch.
- Rebuilt Connector outbox draining as a Condition/generation single flight.
  A nudge arriving while an owner is publishing advances the generation, and
  that owner must rescan it before becoming idle. Busy calls no longer lose
  wakeups or create unbounded publisher work.
- Runs synchronous Connector publishers in one bounded daemon attempt. On
  timeout the heartbeat stops, the durable row remains pending and a
  secret-free `stuck` circuit prevents thread accumulation. A late thread can
  acknowledge only with its original unexpired lease token and a healthy
  Runtime permit; event consumers still deduplicate the immutable `event_id`.
- Uses a Condition seqlock for system outbox health so durable pending count
  cannot be combined with a different active/generation state. Sustained churn
  falls back to one bounded unified fence, and inactive backlog is degraded,
  never falsely ready.
- Shutdown now orders producers/workers, durable outbox flush, Runtime Gate
  closure, then adapters/transports. Maintenance, request nudges and final
  flush use dedicated daemon runners rather than the default executor, and all
  waits share the lifecycle monotonic deadline. A publisher that first hangs
  during final flush cannot stretch a 0.2-second lifecycle budget to its own
  two-second timeout; pending work survives restart.
- Connector login consume paths now require explicit Runtime control
  admission. Late-success watchers start from a clean Context with a fresh
  Connector permit, and same-idempotency retries use bounded durable
  reconciliation without a second provider dispatch.

Red-team evidence covered 80 repeated epoch/stuck/idle-boundary cases, ten
old-daemon/new-owner races, ten sink-success/Gate-close races, child-process
hard-deadline shutdown and a 20-row backlog. Connector plus Runtime shutdown
and observability adjacency finished with 150 passing tests.

## 2026-07-12 - Product startup, handler authority and migration concurrency

- Finished real product Phase A/Phase B startup. Managed Session, Device,
  Update and Extension services support projection-only construction and
  explicit healthy convergence; Critical startup performs no semantic
  business write. Repeated full product construction is idempotent.
- Upgraded Windows workspace attestation to `stable-provision-v2`: immutable
  payloads retain full-tree identity, while mutable workspace roots bind a
  stable security policy and verify every child for AppContainer ACL, Low
  Integrity and reparse/symlink rejection. A first boot creating Outputs no
  longer invalidates the second boot; old v1 attestation cannot be reused.
- Corrected Core handler availability after product binding. The four
  Connector discovery/call handlers and `artifact_read` clear only a stale
  `verified_handler_not_installed` or their own not-bound fact. Administrator,
  offline, sandbox and Pack denials are never cleared or overwritten, and no
  injected handler may replace these Core owners.
- Root-caused concurrent Cloud Share migration failure to the transaction-free
  `PRAGMA journal_mode=WAL` after a successful exclusive commit. WAL activation
  now retries only SQLite BUSY/LOCKED with a five-second bounded exponential
  backoff, verifies the resulting mode, and fails other I/O immediately.
  Barrier stress completed 300 rounds by eight callers (2,400 migrations)
  with one history row and WAL reasserted by every caller.

## 2026-07-12 - Frozen local GA verification checkpoint

- The authoritative managed chat route remains `ecorex-chat` to
  `gpt-5.6-sol`, `reasoning.effort=medium` and exact server-side compaction
  threshold 272000. The provider secret adapter is unchanged; the local
  Runtime receives only a managed-session bearer and never a provider API key.
- The final Python v1 run completed 1,769 passed, 17 platform-condition skips,
  zero failures and zero errors. Its persistent JUnit and output are under
  `.candidate/quality/full-pytest-20260712-175948.*`.
- Final Web evidence is 158/158 Node contract tests, clean TypeScript, 25/25
  Playwright scenarios, 2,080 transformed modules and a content-addressed
  bundle at 471.92 KiB initial JavaScript, below the unchanged 475 KiB limit.
- Runtime/server schema, strict design, legacy cutoff, dependency-lock, public
  download, local reproducibility, generated-contract, lint/compile and
  whitespace gates pass. The source-tree gate intentionally remains red:
  605 of 608 authoritative files are not yet tracked. Their content pre-scan
  found no symlink, binary, non-UTF-8, CRLF, missing LF or trailing whitespace,
  but only Git admission followed by a fresh gate run can close this release
  condition.

No Candidate was published or rollout changed. Protected Windows/macOS
platform artifacts, real Gateway/Connector credentials, public mirror/CDN
readback and multi-hour production soaks remain external release evidence.

## 2026-07-12 - WebUI product-language and task-continuation closure

- Routed Interaction, task inspection, precise retouch and Extension failures
  through the controlled Chinese error boundary. A server API error can now
  expose only an approved code/status message; its arbitrary response text is
  never reused as primary UI copy. Retouch failure reasons use the same
  allowlisted service-reason projection.
- Renamed the task Replay surface to “任务检查与重新运行”. Record digests,
  cursor positions, work-step IDs and permission IDs are collapsed under
  explicit technical detail. The normal view uses task, work-step, saved
  record and rerun language; Extension and Settings similarly replace MCP,
  runtime, manifest and lease vocabulary with user-facing labels.
- Added a four-task browser fixture with independent projections and a delayed
  historical task. Sidebar navigation now disables only the selected row, so
  a newer choice can abort an older read; the existing generation fence proves
  the late old response cannot overwrite the latest task. A missing manual ID
  preserves the original transcript, while Enter and the mobile drawer restore
  a valid task.
- Exercised default Output alias selection, learned-memory reset/undo and
  complete-access enable/revoke through the real Settings UI. Added a real
  task-inspection flow that checks saved records, confirms rerun and observes
  the newly created work step while technical identifiers remain folded.

Evidence: TypeScript and generated contracts passed; `npm run test:v1` passed
161/161; `npm run test:e2e` passed 30/30 Chromium scenarios; the production
build transformed 2,080 modules into 17 content-addressed assets at 472.60 KiB
initial JavaScript; the strict design gate retained zero violations. No Runtime
Python, publication, rollout or Candidate state was changed by this closure.

## 2026-07-12 - Artifact transaction and system-observability closure

- Added a same-database `persist_in_transaction` contract for Artifact event
  intents. Feedback, open/reveal receipts, direct Retouch requests and Retouch
  workspace completion now commit their business rows and immutable outbox
  intent together; publication starts only after SQLite commits.
- Joined Retouch workspace `submitted`, the public Retouch Job, its internal
  annotation layer, optional Durable Job binding and the event intent in one
  transaction. Intent failure rolls the complete unit back; publisher failure
  retains one pending, idempotent outbox row for restart drain.
- Fenced persisted system-health samples with a Runtime admission permit and
  the database commit guard through the actual commit. A Critical transition
  at the dirty-commit boundary now rolls the sample back.
- Extended the unified technical health projection with real Audit, Trace,
  Share, Retouch, Device Authorization, image-publication and Artifact-event
  queue/supervisor state. Disabled providers remain explicit without degrading
  the core Runtime.
- Removed cached executable residue from retired WebChannel/admin trees and
  made the legacy cutoff inspect `.pyc`, `__pycache__` and every other file in
  retired trees. Only static notes in dedicated `docs`/`history` subtrees are
  exempt.

Fault-injection and adjacency evidence completed with 140 passing tests. The
focused Ruff check, Python compile checks and strict legacy cutoff also passed.
No release, publication, rollout or Git commit was performed.

## 2026-07-12 - Runtime streaming checkpoint and Event delivery performance

- Replaced per-delta Agent Job heartbeat writes with a per-run checkpoint
  pulse. Reasoning and answer deltas always update the newest in-memory
  recovery checkpoint, while durable heartbeat/checkpoint writes are limited
  to a configurable 100–250 ms interval (200 ms by default). Model terminal,
  tool-call and failure boundaries force the latest checkpoint immediately.
- Kept the existing silent-provider lease loop. A provider that produces no
  event still renews at the lease-derived interval with the newest checkpoint;
  every write retains worker ID, lease token, execution permit and commit
  guard validation. Replayed deltas retain their original idempotency keys.
- Added a process-local, database-path-scoped Event notification hub. New
  Event facts register a callback on the owned SQLite connection and wake
  local thread subscribers only after the transaction commits. Rollback clears
  the callback; duplicate idempotent reads do not publish a false append.
- SSE captures a notification generation before each SQLite page read. It
  always reads the fact source first, then waits on the generation, closing the
  page-to-wait lost-wakeup window. A one-second SQLite poll remains for another
  process, disconnect detection and keepalive; cancellation removes waiters.

Evidence: 128 mixed reasoning/text deltas completed with fewer than one-quarter
as many heartbeat facts, while the terminal checkpoint held the final sequence;
silent-provider renewal and contender exclusion passed. Five notification
race/pressure tests covered commit/rollback, two EventStore instances, 24
waiters, multiple threads, cancellation, 16 SSE clients, page-to-wait injection
and notification-free fallback. Worker/Event/Kernel adjacency passed 89 tests;
lease/state/shutdown fencing passed 43; tool/HITL replay fencing passed 18;
focused Ruff passed. One cold child-process wall-clock assertion was 3.687 s
against a 3.5 s host bound on its first isolated run and passed both immediate
rerun and the complete 43-test rerun. No release, update, observability or
Artifact-agent implementation was changed.

## 2026-07-12 - Cross-transaction download CAS and administrator rollback authority

- Added a product-scoped verified download CAS shared by install transactions.
  Core, delta and Capability Pack downloads enter it only after manifest,
  artifact signature, exact size and SHA-256 verification. Publication uses an
  atomic replace; materialization re-verifies into a transaction-private file.
  Per-digest product locks provide cross-process single-flight behavior, while
  corrupt entries are quarantined and bounded age/capacity collection skips
  live leases. A cancelled transaction can reuse already verified bytes without
  trusting its abandoned staging directory.
- Added a first-class administrator rollback record beside normal rollouts.
  Creation is idempotent and audited, and accepts only a published older target
  that has a prior non-draft normal rollout and the exact same canonical Core
  platform/architecture matrix as the source. Activation, pause and halt have
  rollback-specific API/audit identities and still use the durable rollout
  wake signal and channel kill switch.
- Added a compact Ed25519 rollback authorization distinct from the immutable
  release trust role. It binds the authenticated client, exact source release,
  build and artifact, exact target release/build/artifact, channel, platform,
  architecture, request nonce and a 60–900 second lifetime. The HTTPS feed
  verifies the nonce-bound grant before exposing it; the installer re-verifies
  and atomically consumes the locally accepted fingerprint exactly once.
- Runtime feed and WebSocket requests now project the re-verified current slot
  release/build identity. A matching active rollback outranks normal upgrades
  only for that source build. The signed target then follows the existing
  prepare, user confirmation, drain, atomic activation and health chain; no
  rollback-specific activation bypass was introduced.
- Added `rollback_public_keys` to the signed Product Runtime configuration and
  enforced release/rollback key-ID and key-material separation. Production
  Control Plane composition reuses its separately trusted online publication
  signer for short-lived rollback authorization, never the offline release key.
- Added an administrator Web console rollback form with targeting, bounded TTL,
  confirmation, activate/pause/halt controls and content-addressed asset hashes.

Evidence: the focused update/CAS group passed 56 tests with two platform skips;
the Control Plane schema, client, UI, signal, production and release-flow group
passed 88 tests; rollback token, API, WSS hint and Runtime safe-activation
coverage passed; focused Product Runtime/configuration coverage passed eight
tests. Ruff, Python compile checks and JavaScript syntax validation passed. No
release, rollout, external publication or Git commit was performed.

## 2026-07-12 - Final local productization closure and evidence correction

- Froze the Candidate topology at six required Capability Packs for every
  target: `browser`, `channels`, `image`, `ocr`, `office` and `sandbox`. Core,
  Bootstrap and those six packs produce eight receipts per target and 24
  receipts across the three supported target tuples. `channels.adapters`,
  `ocr.extract` and `office.formats` are service-only bindings and expose no
  invented tools. In particular, `office.formats` proves bounded
  create/read/validate support for DOCX, XLSX, PPTX and PDF; it does not claim
  to be a high-fidelity Office renderer.
- Bound Runtime admission to the durable update activation boundary. Once the
  active pointer may have switched, including an interrupt after atomic slot
  replacement, the old Runtime remains drained; an unreadable boundary also
  fails closed. A pre-boundary timeout returns the transaction to
  `awaiting_user` without discarding the staged candidate.
- Completed the cross-transaction verified download CAS and administrator
  rollback path. Core, delta and pack bytes are admitted only after signature,
  exact-size and SHA-256 checks, reused through digest-scoped single-flight,
  quarantined on corruption and materialized into transaction-private staging.
  A rollback remains an audited, short-lived, nonce-bound authorization to an
  older compatible known-good release and still follows user confirmation,
  drain, activation and health checking.
- Corrected Runtime supervisor backoff to use an absolute monotonic deadline.
  Early platform timer wake-ups now wait for the remaining interval while an
  explicit stop still interrupts immediately.
- Made the streaming checkpoint pulse accept an injected monotonic clock and
  start the next heartbeat window after its durable commit completes. The
  pressure assertion now derives its bound from measured elapsed time instead
  of assuming an unloaded host; the 100–250 ms product checkpoint contract and
  forced terminal/tool boundaries are unchanged.
- Split Connector invocation into bounded local admission and provider
  response phases. Limiter wait plus the final policy/SQLite fence no longer
  consume the provider response timeout. A pre-dispatch timeout cancels and
  joins the task before it can cross into the adapter; a post-dispatch mutating
  timeout retains the operation fence and late-result reconciliation, avoiding
  an untracked external write.

The first final Python run exposed a Windows timer early-wake boundary. After
that root fix, the second run exposed the host-speed-dependent heartbeat
assertion and the Connector admission/provider timeout race. Those were fixed
at their authority boundaries and independently repeated before the final full
run. The final local Python result is **1,814 passed, 17 skipped, 0 failed**.
The final Web result is **161 unit/contract tests**, clean TypeScript,
content-addressed production build, and **30 Playwright scenarios**. Runtime
and server schema authority, design-system debt, legacy cutoff, dependency
locks, public-download shape, reproducibility, supply-chain preflight and
whitespace gates passed.

The remaining local release blocker is intentionally the source-tree Git
admission gate: 623 authoritative v1 files were inventoried, of which 3 are
tracked and 620 remain untracked. An independent scan found zero regular-file,
UTF-8, LF, final-newline or trailing-whitespace violations; no file was
implicitly staged, committed or pushed. This workstation has no Go toolchain,
and no protected clean runner, KMS/signing authority, live release origin or
live Connector/model endpoints were available. Therefore this record does not
claim Bootstrap Go-test completion, protected Candidate evidence, external
signature/publication, live-provider certification or rollout completion.

## 2026-07-12 - Authorized local Git admission boundary

The user authorized local source admission and a fresh build for manual
acceptance, while explicitly withholding push and publication until that
manual test passes. The complete change set was staged after excluding
`.candidate/` logs, JUnit output and temporary reports. The staged scope has
no file larger than 10 MiB and contains no build cache, `desktop/dist`,
`node_modules` or temporary Candidate path.

The real source-tree gate now passes with all 623 authoritative v1 files in
the Git index. Candidate supply-chain preflight, v1 lint/compile, 20 Runtime
schema fragments, 8 server authorities across 3 roots, design debt, dependency
locks, legacy cutoff and staged whitespace checks all pass. This changes the
local boundary from source admission to fresh Candidate construction; it does
not authorize a push, release publication or rollout.

## 2026-07-12 - Local Candidate trust-role drift correction

The first post-admission Windows signed-Candidate drill failed before staging
with `ProductRuntimeConfigurationError`. The Runtime configuration authority
now requires a rollback verification keyring separate from release signing,
but the local drill still emitted the earlier two-role release/session shape.
The drill now creates independent process-only Ed25519 release, rollback and
session keys, writes all three public roles, includes every private value in
the persistence scan and never serializes private material. Seven focused
drill tests, Ruff and Python compilation pass. No installation, activation,
external request, push or publication occurred during the rejected attempt.

The repeated committed-source drill then reached real Pack staging but was
terminated at 1,804 seconds while the bounded stager was still making forward
progress. The protected job already allowed 60 minutes, while its repository
wrapper allowed 35 minutes and the nested stager only 30. Those budgets are
now ordered at 45 minutes for staging, 50 minutes for wrapper verification and
60 minutes for the protected job, retaining hard process-tree termination and
a ten-minute cleanup/receipt margin. Seventeen focused process-boundary and
Windows drill tests pass; this is a timeout-contract correction, not a skipped
probe or an unbounded build.

## 2026-07-13 - v0.3 workbench expansion and backend-owned projects

The WebUI restoration boundary was expanded from outer geometry to the full
v0.3 workbench interaction model. Runtime now owns a typed ProjectService and
`/api/v1/projects` catalog, validates native folder selections, deduplicates
canonical paths and replaces forged project display metadata before a new
Thread is created. The first-message outbox freezes that authoritative project
binding so a retry cannot jump from a project conversation into a general one.

The React shell now exposes project folders, nested project sessions, general
sessions, search and continue-by-ID navigation. The empty transcript lets the
user choose a general or project conversation before the first message. Chat
rows no longer render Agent or user avatars. Model selection, permission state
and the v0.3 daily/weekly/context meter positions moved into the Composer.
Missing usage dimensions render an explicit dash; the UI does not estimate
tokens or invent daily/weekly quota facts.

Sidebar was split into a local deferred chunk after expanded navigation crossed
the existing initial-JavaScript budget. The production gate passes at 467.14
KiB initial JS (144.86 KiB gzip) without increasing the budget. A real browser
pass found and corrected a GA fixture drift where the mock returned `items`
instead of the authoritative `projects` field. Project selection, first-message
creation, avatarless transcript rendering and the in-Composer GPT-5.6 SOL
selector were then exercised successfully. File upload and authoritative usage
aggregation remain unfinished; release remains blocked.

## 2026-07-13 - Composer attachments, provider usage and message-width closure

The Composer `+` control is now a complete Runtime-owned input-attachment
chain. The browser uploads a bounded file once, receives an opaque
account-scoped attachment/revision projection, and only persists that safe
reference for durable retry; it never persists bytes, absolute paths,
credentials or provider output. Runtime stores the source as an internal
Artifact, binds it atomically into the accepting Turn, exposes a segmented
read tool only for that bound Turn and rejects cross-account, unbound or
idempotency-conflicting input. Images retain their opaque identity for the
vision/image path rather than being injected into a prompt.

`input_attachment_read` is deferred in ordinary Turns. A Core-owned
`runtime_context_required` planner fact promotes it only when immutable Turn
metadata proves an attachment is bound. This preserves progressive tool
disclosure and avoids pretending that the user manually selected a tool.

The restored Composer meter is now authoritative: `GET
/api/v1/threads/{id}/usage` aggregates provider-reported completed-response
facts for the local calendar day and week, and displays the latest actual
input-context count against the selected model's signed 272k compaction
threshold. The visible managed-service allowance remains the signed Bootstrap
quota. No browser-side token estimate or invented period quota is used.

Real browser inspection also exposed a pre-existing CJK layout fault: user
message bubbles inherited inline-size containment intended for fixed-width
assistant rows, causing short text to collapse into a vertical strip. The
containment is now removed only for shrink-to-fit user bubbles, with a focused
browser regression.

## 2026-07-13 - Composer placement, compact navigation and mobile queue closure

Composer placement is now an explicit conversation-mode contract rather than a
side effect of CSS Grid auto-placement. A new general/project conversation
passes the same Composer into the centered chooser surface. Once a Thread
exists, the Composer moves into a dedicated `workspace-bottom` region and
remains at the actual bottom of the Workspace.

The underlying layout defect was an empty status stack using `display: none`:
without explicit grid slots, the Timeline and Composer were re-auto-placed into
earlier rows and a large blank area appeared beneath the Composer. Header,
status, Timeline and bottom actions now own fixed grid rows, so hiding the
status stack cannot change semantic layout order.

On 320px touch screens, an active Turn's disposition, stop action and send
action now use a bounded three-column layout. The send action keeps an explicit
accessible name while its redundant text label becomes visually compact; it no
longer falls outside the viewport before a queue request is sent. The compact
sidebar now gives project/session toggles explicit accessible names and renders
an icon-plus-semantic-label project-session creation action instead of leaving
low-contrast text compressed inside the 88px rail.

The first complete E2E rerun surfaced these compact-sidebar accessibility
defects at 1024px and failed closed. They were corrected before a second full
run; no timeout, assertion relaxation, or production deployment was used to
turn that result green.

## 2026-07-13 - Attachment-runtime availability and full local candidate gate

The post-Composer candidate run found a real consistency fault instead of
treating three changed catalog assertions as a reason to simply relax tests.
`RuntimeComposition` correctly bound the trusted `input_attachment_read`
handler when the Artifact service existed, but its `RuntimeAvailability`
projection retained the lower-level `verified_handler_not_installed` fact.
That meant a Turn with an immutable, backend-bound upload could promote the
reader to direct exposure while availability still rejected it.

Runtime now reconciles that specific stale handler-absence fact after binding
the Core reader, both at startup and when a dynamic availability projection is
sampled. It never clears an administrator, policy, sandbox or offline denial.
Without a reader, the projection now explicitly reports
`input_attachment_runtime_not_bound`; with a reader it remains deferred for
ordinary Turns and is promoted only by the immutable per-Turn attachment fact.
The tests cover both halves of that contract, in addition to the product App's
actual handler binding.

The complete Python gate was restarted as a background process after the
interactive command channel enforced its 60-second limit. This was a transport
constraint, not a test result: the completed JUnit run reports 1,836 passed,
17 platform-conditioned skips and five third-party deprecation warnings in
745.98 seconds. Source-tree, lint/compile, whitespace and local supply-chain
preflight passed against the resulting source. No signed Candidate was
activated, pushed or published.

## 2026-07-13 - Windows signed-candidate provisional-startup closure

The first zero-publication Windows x64 signed-candidate ceremony on commit
`75ac7b49` completed all eight local stage receipts (Core, Bootstrap and the
six required Packs) and reached a real first-install activation. It failed
closed while the provisional Runtime was still becoming healthy: the Bootstrap
reported no child exit code or startup error, only that the 30-second loopback
window elapsed before readiness. The disposable install root and process-local
keys were removed; no user slot, release or publication was touched.

The first response was to extend the bounded probe budget to 90 seconds. A
second source-pinned ceremony also failed at that exact boundary, proving that
the root cause was not merely an undersized timeout: the provisional Runtime
was redundantly re-hashing the complete Browser/OCR/other Pack set that the
Bootstrap had just verified before launch.

The product boundary is now explicit. A provisional process verifies its signed
slot identity, sandbox attestation, Web bundle and nonce-bound activation proof
only; it does not open the platform credential vault or reconstruct Pack
adapters while it exposes no business endpoint. Bootstrap remains responsible
for exact Pack-content verification immediately before launch. After
confirmation, the full Runtime still verifies and binds every Pack before it
can cross the data barrier or serve user traffic, preserving pre-data rollback
on failure. The normal loopback proof budget is therefore restored to a short,
bounded 30 seconds (60-second maximum). Focused Bootstrap/activation coverage
passes; a fresh source-pinned signed candidate ceremony is required before this
change can earn a Candidate receipt.

## 2026-07-13 - Signed Runtime startup-stage diagnostics

The first post-separation Windows ceremony proved the provisional activation
probe itself: it created the activation receipt inside the ordinary 30-second
window and safely fell back from the local GitHub-CN mirror fixture to the
GitHub fixture. The subsequent full Runtime then exited with the bounded
configuration code `64` before ordinary HTTP readiness. The old Bootstrap
intentionally discarded child stderr, so the ceremony exposed an exit code but
not the fixed startup stage needed to distinguish a Pack binding fault from a
credential-vault or composition fault. The disposable install root was removed
and no Candidate receipt was produced.

Runtime and Bootstrap now exchange only a one-shot, nonce-bound fixed stage
code through an internal advisory file under the signed install root. The
Runtime can record a whitelisted stage such as `credential_vault`,
`capability_pack_binding` or `update_runtime`; it never writes an exception,
provider response, filesystem path, token or process argument. Bootstrap reads
and deletes the matching file only after child exit, returns it as
`runtime_startup_stage` for observability, and never uses it to select,
confirm, roll back or trust a slot. The configuration path also separates
credential-vault construction from immutable Pack binding so that the safe
stage reflects the actual boundary.

Focused Bootstrap, product-CLI, activation and signed-drill regression covers
the nonce binding, deletion, stage redaction and unchanged probe-only/full
Runtime split. A new source-pinned local ceremony is required to identify and
fix the remaining full-Runtime root cause; no timeout has been enlarged and no
failed evidence is counted as a Candidate pass.

## 2026-07-13 - Browser Pack canonical descriptor and startup-scan closure

The next source-pinned Windows ceremony from `326526fb` completed the real
Core, Bootstrap and six-Pack platform stage, generated all eight local stage
receipts, migrated the released v0.3 schema copy-on-write and confirmed the
nonce-bound first-install probe. The following full business Runtime failed
closed with configuration exit `64`; the new bounded diagnostic identified
`capability_pack_browser`. The disposable slot and process-local signing keys
were removed, no report was promoted, and no release endpoint was contacted.

A minimal reproduction then built the real 190,153,573-byte Browser Pack with
the locked Playwright 1.52.0/Chromium closure and passed it directly through
the Runtime ZipApp inspector. It returned `pack_descriptor_invalid` in
161.312 seconds. The signed Pack contained a 129-byte `ecorex-pack.json`; its
semantic content was correct, but the source-file LF made it differ from the
128-byte canonical Runtime contract. An older retained real Core independently
verified all 1,733 Pack-Python closure files and 61,188,898 bytes against its
manifest, ruling out the closure algorithm as the descriptor failure.

Platform staging now treats process-Pack descriptors as generated wire
artifacts. It validates the source template against the authoritative Pack
catalog, writes exact sorted compact UTF-8 bytes without a trailing newline,
and makes both Browser and Sandbox gates re-read that exact form before
signing. Runtime's strict comparison is unchanged. A semantically drifted
template still fails closed instead of being overwritten.

The diagnosis also exposed unnecessary cold-start amplification. Browser and
Sandbox previously rescanned the same signed relocatable Python closure in one
Runtime composition, while the platform stager scanned it three times in one
stage. Production CLI now creates a resolver whose cache exists only for one
synchronous composition: one startup verifies once, a restart verifies again.
The stager retains its independent post-write verification and reuses that
identity for later gates, eliminating only the third scan. No persisted or
cross-process trust cache was introduced.

A fresh post-fix real Browser Pack then completed the same inspection path. It
was 190,153,571 bytes, contained the exact 128-byte descriptor without LF, and
the production adapter bound exactly `cdp` and `fetch` in 116.969 seconds. This
is focused root-cause evidence, not a signed Candidate receipt; the complete
zero-publication ceremony must still be rerun from a committed source identity.

## 2026-07-13 - Full Runtime application-composition diagnostic boundary

The sixth zero-publication Windows ceremony ran from full source identity
`ada2c1f5fdf825df5edc10b193fc626ac7df408b`. It generated all eight Windows
stage receipts, retained the exact 190,153,571-byte Browser artifact, completed
the released-v0.3 copy-on-write migration and confirmed the nonce-bound
first-install probe. The following full Runtime advanced beyond
`capability_pack_browser`, proving the descriptor/Pack-Python correction in the
integrated chain, then exited safely at the previous aggregate stage
`server_configuration`. The disposable root was removed, the repository
remained clean and no Candidate report or release was published.

The aggregate stage covered three materially different boundaries: loading the
signed Runtime composition, constructing the FastAPI application, and building
the loopback Uvicorn configuration. The product entrypoint now maps only
configuration/value failures at those boundaries to the fixed redacted stages
`runtime_composition`, `application_composition` and
`http_server_configuration`. Existing loader stages and trust failures retain
their stronger identities; no native exception text, path or provider detail
crosses the process boundary.

The same correction closes a resource-lifecycle gap. Managed transports are no
longer transferred to application ownership before Uvicorn configuration has
successfully completed. A synchronous application or HTTP configuration
failure closes the unstarted composition exactly once. Product-entrypoint
regression is 32 passed and one platform-conditioned skip; the broader
entrypoint, Bootstrap supervisor, activation-health and signed-drill set is 87
passed and two skips, with dedicated checks for all three stage mappings,
redaction and pre-transfer cleanup. A
fresh committed ceremony is still required to identify and then correct the
specific application layer; this diagnostic refinement is not a Candidate
pass.

## 2026-07-13 - Signed Core owns its IANA timezone database

The seventh zero-publication Windows ceremony ran from committed source
`f6ca3ff1`. It again generated all eight Windows platform-stage receipts,
completed the released-v0.3 copy-on-write migration and passed the nonce-bound
provisional activation probe. The full Runtime then failed closed with exit
code `64` at the newly isolated `application_composition` stage. The retained
disposable slot was used only for bounded diagnosis; no report, release or
publication endpoint was written.

The same slot succeeded through signed Runtime composition when loaded from
the source interpreter, but failed while constructing the FastAPI application
under its packaged interpreter. The exact nested cause was
`ZoneInfoNotFoundError` for `Asia/Shanghai`, caused by the packaged Core not
containing the `tzdata` distribution. Windows does not provide a system IANA
timezone database, while the developer interpreter happened to have one. This
made the source-tree result a false proxy for the signed product environment.
Hashes and file counts confirmed that the relevant packaged EcoreX modules
matched source; credential-vault and Capability Pack composition were ruled
out before changing code.

Core now pins and carries `tzdata==2026.2` in the product dependency source,
all affected hash locks and the platform-stager Runtime closure. Because the
native launcher correctly uses Python isolated mode and therefore ignores
`PYTHON*` environment variables, both the Core probe and product server
explicitly call `zoneinfo.reset_tzpath(())` and clear the zone cache. The
Bootstrap environment also sets an empty `PYTHONTZPATH` for non-isolated
diagnostic paths. The Core probe imports `tzdata` and resolves
`Asia/Shanghai` from the generated `pack-python` before any stage receipt can
be accepted. There is deliberately no UTC or fixed-offset fallback: such a
fallback would silently corrupt day/week usage windows and future daylight-
saving behavior.

Lock admission, product entrypoint, Bootstrap environment, usage projection
and platform-staging regression passed 111 tests with three platform-
conditioned skips; Ruff,
compilation and whitespace checks passed. A separate disposable full-closure
proof was intentionally uncredited after the command boundary terminated it
at 904 seconds before the stager emitted a result. Its 61,841,500-byte partial
tree was safely removed after the child released its file handles. The next
committed full ceremony remains the authoritative packaged-Core proof and must
also complete install, update, bad-digest and rollback gates before this work
can earn a Candidate receipt.

The dependency addition also exposed a supply-chain completeness defect. The
preflight license collector had six old Runtime roots hard-coded and checked
only that collected packages matched the lock; it did not require every locked
package to appear in the license evidence. It could therefore report success
with 22 locked packages but only 21 licensed packages. The Runtime lock is now
the sole input set for Python license collection, and exact canonical-name and
version equality is mandatory in both directions. Candidate pipeline
regression passed 15 tests. The corrected preflight records all 22 Runtime
packages, including `tzdata 2026.2` as `Apache-2.0`, and passed the 449-file
secret inventory with report
`.candidate/quality/supply-chain-local-tzdata-fix-v3.json`. The final combined
Candidate/release-integrity, dependency, entrypoint, Bootstrap, usage and
platform-staging gate passed 134 tests with three platform-conditioned skips;
the authoritative source-tree check reports 632 files.

## 2026-07-13 - Signed Core owns its multipart route dependency

The eighth zero-publication Windows ceremony ran from committed source
`3c78d1d0`. The rebuilt Core passed its isolated signed-IANA probe, all eight
Windows stage receipts were emitted, released-v0.3 data migrated copy-on-write
and the nonce-bound provisional activation health check passed. The subsequent
full business Runtime remained before the data barrier and eventually exited
with code `70` at the aggregate safe stage `software`. Bootstrap correctly
revoked the provisional sandbox authorization and restored the empty first-
install slot pointers. No Candidate report, release endpoint or publication
endpoint was written.

The retained disposable slot was diagnosed with its own hash-verified
`pack-python` interpreter in isolated mode. Because rollback had intentionally
revoked the slot's sandbox attestation, the diagnostic temporarily bypassed
only `WindowsSandboxSlotSecurity.validate` in that one diagnostic process,
restored the signed pointer for composition and restored the empty pointer on
exit. It did not change a signed byte, re-authorize the slot, serve traffic or
qualify as Candidate evidence. The exact failure was a
`ModuleNotFoundError` raised by FastAPI while registering multipart form routes:
`python-multipart` existed only in the developer extra, so the source
interpreter again masked an incomplete signed Core closure. The earlier
working-set observation was therefore not the root cause.

The already reviewed `python-multipart==0.0.26` baseline is now a direct
Runtime dependency rather than a developer-only dependency. Runtime, Cloud
and platform-stage locks carry the same exact hashes; no unrelated dependency
was upgraded. The Core closure contains the distribution, and its isolated
probe imports FastAPI's required `multipart.multipart.parse_options_header`
before a Core receipt can be accepted. FastAPI application-construction
`RuntimeError` is also normalized to the existing redacted
`application_composition` stage, so a future route dependency failure remains
actionable without exposing native exception text.

The exact import reports version `0.0.26`. Product entrypoint, platform-stage
and reproducibility regression passed 89 tests with two platform-conditioned
skips; product ASGI, upload, attachment, Artifact API, dependency-lock and
Bootstrap regression passed 55 tests with two skips. Ruff, dependency-lock,
source-tree, Runtime/server schema-authority and design-system gates passed.
The corrected supply-chain preflight records 23 Runtime packages = 23 licensed
packages, including `python-multipart 0.0.26 / Apache-2.0`, and scans 449
production files. Its report is
`.candidate/quality/supply-chain-local-multipart-fix.json`. A fresh committed
ninth ceremony must rebuild the signed Core and complete install, migration,
update, bad-digest and rollback gates; the retained failed slot cannot prove
this correction by mutation.

## 2026-07-13 - Candidate timing hierarchy matches multi-activation scope

The ninth zero-publication Windows ceremony ran from committed source
`80a4d6c8` with the former explicit 3,600-second total limit. It emitted all
eight Windows receipts, and its Core dependency evidence contained all 23
locked Runtime distributions including `python-multipart 0.0.26` and
`tzdata 2026.2`. The isolated Core identity covered 2,388 files and
61,937,031 bytes with closure digest
`68742f27189c69c16f68de846f20074f9093d83cefc6aba03bf16c423fce1822`.
The first full business Runtime successfully crossed Capability Pack binding,
FastAPI application composition and HTTP readiness, proving the multipart
correction in the real signed product environment. Released-v0.3 migration
completed and the source was then removed.

The ceremony subsequently exhausted its total deadline while the second full
Runtime was still making measurable progress during the post-migration source-
removal restart. At timeout the activation receipt was `confirmed` and the
expected slot was current and known-good; failed-ceremony cleanup then revoked
its temporary sandbox authorization and restored the disposable first-install
pointers to empty. No Candidate report, healthy-update receipt, rollback
receipt or publication was created. This was not a Runtime crash: the old
single deadline combined a 40-minute cold platform build with four independent
full Runtime readiness exercises plus release/update work, while the public
CLI help incorrectly advertised a 3,600-second maximum even though the code
already admitted 5,400 seconds.

The local ceremony now uses a bounded hierarchy. Its truthful default and
maximum total are 5,400 seconds; the source-pinned platform-stage wrapper has
an independent 3,000-second ceiling, and every one of the four full Runtime
readiness exercises receives a fresh 900-second ceiling that can never outlive
the total ceremony. A stalled Runtime therefore cannot consume the remaining
update/rollback budget, while normal restarts no longer inherit only the few
minutes left after a cold platform build. Successful reports include this
deadline policy as evidence. Focused Candidate, process-boundary and Bootstrap
regression passed 49 tests with one platform-conditioned skip; Ruff, help-text
and whitespace checks passed. The expanded Candidate release, storage-
migration, process-boundary and Bootstrap set passed 82 tests with two skips.
Current-source supply-chain preflight again records 23 locked = 23 licensed
Runtime packages and 449 scanned files with inventory digest
`d0f6e32a8c877cc8666491b5c700e64dcca8d6b756139f3f23df80664e18e96c` in
`.candidate/quality/supply-chain-local-deadline-policy-v2.json`. A fresh committed
tenth ceremony must still complete healthy update, bad-digest rejection and
pre-data rollback before a local Candidate receipt exists.

## 2026-07-13 - Tenth ceremony coverage and Runtime trust-scan performance

The tenth zero-publication Windows ceremony ran from committed source
`078d0e81` with the nested 5,400-second total, 3,000-second platform phase and
independent 900-second Runtime windows. It emitted all eight Windows receipts
and reproduced the exact 2,388-file / 61,937,031-byte Core interpreter closure.
First install used the domestic-mirror-first source order, rejected the
injected first-source failure, fell back to GitHub, stopped at
`awaiting_user`, and activated only after explicit confirmation. The full
signed Runtime reached HTTP readiness. Released-v0.3 data then migrated
copy-on-write, registration pinning ended, the legacy source was removed, and
the source-removed Runtime restart completed.

The same ceremony built a distinct healthy update, stopped its background
download at `awaiting_user`, activated it after confirmation and completed its
full Runtime health. A bad-digest artifact was rejected before pointer
mutation. A separately signed fault candidate activated provisionally, failed
before the data barrier and produced `bootstrap_health_failed_rolled_back`;
the prior healthy slot was again both `current` and `known_good`, while the
fault slot was no longer current. The final recovered Runtime was still making
progress when the aggregate 90-minute deadline expired during the last
rollback-health wait. Failure cleanup restored the disposable pointers to
empty and left no child process. No Candidate report, release or publication
was created. This is complete functional evidence through rollback pointer
recovery, but not a successful ceremony receipt.

Read-only profiling on the retained tenth slot found the remaining product
problem rather than extending the deadline again. Two consecutive serial
Pack-Python closure scans did not finish within 904 seconds. The closure held
2,388 files but only 61.9 MB, proving that Windows small-file open/metadata
cost, not byte throughput or ASGI/model traffic, dominated. A representative
temporary compaction moved 1,952 zip-safe members into a 5,502,287-byte
`python311.zip`, reduced the physical closure to 437 files / 45,830,062 bytes,
and preserved isolated imports of cryptography, FastAPI, HTTPX, Pydantic,
multipart, tzdata, Uvicorn, WebSockets and EcoreX. The first serial verification
of that compact copy took 181.044 seconds; after the bounded parallel verifier
was introduced, the same closure digest completed in 0.274 seconds and the
slot-tree digest in 0.384 seconds. The original uncompressed 2,388-file closure
also reproduced its exact digest in 1.470 seconds. These focused timings are
diagnostic comparisons on one host, not Candidate readiness receipts.

Platform staging now creates that deterministic CPython import archive only
for zip-safe modules and reviewed resource packages. Native `.pyd/.dll/.exe`
trees and path-sensitive data stay unpacked. Secret scanning explicitly opens
and validates canonical, case-fold-unique archive members, and administrator
assets now use package resources so their exact allowlist works under
zipimport. The isolated Core probe opens both administrator assets and the
zipped `certifi` CA resource. Pack-Python and slot-tree verifiers use at most
16 streaming workers with content, size, object identity, mtime/ctime,
path-mode and reparse-attribute fences plus pre-admission size limits. Slot
receipt validation computes full and Core-only digests from one verified
record set. Nothing is cached across a process boundary.

The focused safety set passed 73 tests with three platform-conditioned skips.
The complete affected platform-staging, process-Pack, atomic install, update
durability/coordinator, Candidate pipeline, product Runtime entrypoint,
administrator Web and signed-drill set passed 182 tests with five skips. Ruff,
Python compilation and zipimport asset regression passed. The current-source
supply-chain preflight records 23 locked = 23 licensed Runtime packages and
449 scanned production files with inventory digest
`7a1c2a1c837c4acb615d7a18e7b65b16cf865dad30a19efad84373fc3d5270e8` in
`.candidate/quality/supply-chain-local-runtime-trust-scan-v2.json`; Runtime and
Server schema-authority plus design-system gates have zero violations. An
eleventh fresh committed zero-publication ceremony is required to measure a
cold signed build and complete the final recovered-Runtime health within the
bounded policy.

## 2026-07-14 - Eleventh ceremony and archive-aware fault candidate

The eleventh zero-publication Windows ceremony started from committed source
`6cd7ccd8` with the unchanged 5,400-second aggregate, 3,000-second platform and
900-second per-Runtime limits. Its cold Core input reached 2,389 physical files,
then atomically compacted to 437 files with a 5,503,338-byte `python311.zip`.
The platform stager completed Core, Bootstrap and all six required Capability
Packs. First install again exercised domestic-mirror-first failure and GitHub
fallback, waited at `awaiting_user`, activated only after explicit confirmation,
reached full Runtime health and completed registration. Released-v0.3 data
migrated copy-on-write, the source-removed Runtime restarted, and a distinct
healthy update was prepared, confirmed, activated and healthchecked.

At 1,450.3 seconds the ceremony failed before building the deliberate fault
release. The fixture still searched only for an unpacked
`ecorex/server/__main__.py`; the production Core now correctly stores that
module in the signed import archive. No Candidate report or publication was
created. Failure cleanup removed the temporary install/stage root and left no
child Runtime. This failure does not invalidate the completed install,
migration or healthy-update observations, but it is not a ceremony receipt.

The fixture now accepts exactly one directory or zipimport entrypoint. Its ZIP
reader rejects non-canonical/case-colliding names, encryption, links/special
files and member/expanded-size excess, rewrites through a same-directory
temporary archive and validates the result. It then regenerates and resolves
`pack-python.json`, ensuring the signed fault Core is internally consistent and
actually reaches the bounded exit-70 module instead of failing on a stale
closure digest. The Windows drill unit suite passes 15 tests; a fresh committed
twelfth zero-publication ceremony is required. The complete affected staging,
Pack, install/update, Candidate, Runtime entrypoint, administrator Web and
Windows-drill suite passes 184 tests with five platform skips. Current-source
supply-chain preflight records 23 locked = 23 licensed Runtime packages and
449 files with inventory digest
`29eab98fe9742876a76c496ddaed9cae2d53055dea7f3124a147cd56c1090ff3` in
`.candidate/quality/supply-chain-local-runtime-trust-scan-v3.json`.

## 2026-07-14 - Twelfth Windows ceremony passed without promotion

The twelfth zero-publication Windows ceremony ran from clean committed source
`7bf9d89b60ea2c8a8881a22bf8d855cc8bf46876` and completed in 1,308.297
seconds under the unchanged 5,400-second aggregate limit. Cold Pack-Python
again compacted from 2,389 physical inputs to 437 files with a 5,503,338-byte
import archive. The production stager emitted all eight Windows x64 receipts;
Core, Bootstrap and all six Capability Packs were newly built and signed.

The install chain exercised domestic-mirror-first failure and GitHub fallback,
background download, `awaiting_user`, explicit activation, durable drain
checkpoints and full Runtime HTTP 200. Released-v0.3 data migrated copy-on-write
and committed, the legacy source was deleted, and the source-removed Runtime
restart returned HTTP 200. A distinct same-version update stayed inactive until
confirmation, then activated and returned HTTP 200. A bad digest was rejected
after all three sources without changing the active slot.

The archive-aware fault Core regenerated and resolved `pack-python.json`,
passed the direct exit-70 preflight, was signed as a distinct release and
activated provisionally. Health failure produced the rollback terminal state,
discarded the fault slot, restored the healthy slot and returned HTTP 200 from
the recovered full Runtime. The disposable root was removed and no child
process remained.

The schema-3 report is
`.candidate/quality/windows-signed-candidate-local-twelfth.json`, SHA-256
`a6d823ccc73a7de28cc6d993e54730e19a64ebcec10173e482d9f8d1b8bcdc0b`.
It explicitly records `promotion_claimed=false`, `fixed_gate_relaxed=false`
and 16 missing protected macOS receipts. This is a successful local Windows
ceremony, not a production Candidate or publication authorization. A small
post-report correction changes only the provenance wording from the inaccurate
static phrase “dirty worktree” to “local workstation” and makes the already
asserted fault exit code / manifest rebound explicit in future reports. The
post-correction current-source supply-chain preflight remains 23 locked = 23
licensed Runtime packages and 449 files, with inventory digest
`df8027e874b8a3b74f5770acd7e951db77eafcc8c9be1f773f7c2d6242374e4a` in
`.candidate/quality/supply-chain-local-runtime-trust-scan-v4.json`.

## 2026-07-14 - Local WebUI acceptance and media-intent closure

The user-confirmed Composer rule is now verified as a conversation-state
invariant rather than a viewport heuristic. A brand-new task keeps the Composer
inside the general/project chooser. Existing tasks, restored tasks and a
project task immediately after its first message render only the workspace-
bottom Composer; the real in-app browser measured a normal-chat bottom delta of
`0.000030517578125px`. The automated E2E contract independently covers the
same transition across the responsive matrix.

A realistic image request exposed a missing product vocabulary unit: selecting
Image 2 and asking to generate a `主视觉` initially stayed on the office path.
The Runtime policy, not the WebUI, now owns the correction. Intent policy
`1.5.0` adds reviewed `主视觉` / `key visual` media-deliverable evidence and a
negative design-plan case. The resulting plan ranks imagegen first while read,
fetch, vision, CDP and shell remain eligible or progressively discoverable.
The local page then generated the image result through the expected image flow.

The same real-user pass found two fixture-contract defects. Retouch completion
used fixed prose unrelated to the submitted annotation; it now derives its
bounded summary from the actual annotation instruction or global instruction.
A post-fix run displayed `只将整体亮度提高 10%...` identically in the chat
result and comparison canvas. Delayed thinking timers could also reopen an
already superseded Turn; both callbacks now require the exact
`model_requested` state, and a replace-before-timer regression waits past both
callbacks to prove the original remains superseded and the replacement queued.

The in-app browser also exercised first-message model selection, the 272k
context projection, image fit/zoom, structured rectangle retouch with normalized
geometry and a reference revision, unique share snapshots with inline/full
images, reasoning replacement without a blank interval, steer/queue/replace,
task-ID continuation, project placement, full-access revoke, memory reset/undo,
output-location persistence, the unified extension catalog and formal Feishu /
Tencent Docs entries. The main, share and raw-image tabs emitted zero console
warnings or errors. Seven screenshots and their hashes are recorded in
`.candidate/quality/cdp/webui-local-acceptance-20260714.json`.

Verification completed with 1,860 Python v1 passes and 17 explicit environment
skips, 162 Web contract passes, 34 Playwright E2E passes, clean TypeScript and
v1 lint, and a successful 18-asset / 17-chunk content-addressed production
build. Live managed inference is still not proven: `gpt-5.6-sol` and
`gpt-image-2` remain provider-unavailable in the current external environment.
No release, deployment or user update was attempted.

## 2026-07-14 - Thirteenth zero-publication Windows ceremony

Because media intent policy `1.5.0` changes signed Runtime behavior, the prior
local Windows receipt could not represent the accepted WebUI/routing batch.
The thirteenth ceremony therefore rebuilt every platform artifact from clean
product commit `89fab32ae0884e9df5549f2b92e3a76d63fe6de1`; no prior Core,
Web bundle, Pack or temporary slot was reused. The source-pinned production
Stager emitted Core, Bootstrap and all six Windows x64 Capability Pack receipts.
The resulting signed release binds 15 artifacts, the 18 immutable Web assets,
build digest
`67c9133deffb9c54c98e146b1d55c042a64505f75eeac9d66818874bcdf5bf4d`
and Web bundle digest
`a19c9094d553b593a8af6c0bd4ad7b6b887b772c03d9a684e916b07573323d99`.

The 1,439.328-second transaction reproduced domestic-mirror failure and local
GitHub fallback, background preparation, `awaiting_user`, explicit first
activation and full Runtime HTTP 200. Released-v0.3 data migrated copy-on-write
to a committed receipt, the source was deleted, and the packaged Runtime
restarted with HTTP 200. A distinct same-version replacement again stayed
inactive until explicit confirmation, then completed and returned HTTP 200.
The injected bad digest was rejected before activation.

The archive-aware fault release regenerated `pack-python.json`, passed the
expected exit-70 preflight, activated provisionally and reached the rollback
terminal state before the data barrier. The fault slot was discarded, the
healthy slot restored, and the recovered full Runtime returned HTTP 200.
Three activation attempts each stopped new admission and persisted a durable
long-job checkpoint first. The disposable directory was removed and no drill
Runtime process remained.

The schema-3 report is
`.candidate/quality/windows-signed-candidate-local-thirteenth.json`, SHA-256
`7144c39a140aa74e91a5a28886a6846b41773d554ded3305102ad36473beccc4`.
It remains evidence class `local-windows-drill`: production preflight is
`blocked`, `promotion_claimed=false`, `fixed_gate_relaxed=false`, and all 16
protected macOS receipts are missing. The report explicitly records that no
live mirror, GitHub Release, CDN, Control Plane, Model/Image Gateway, connector,
OTLP endpoint or tenant credential was contacted. Fifteen post-report drill
tests and the v1 static gate passed. Current-source supply-chain preflight also
passed: 23 locked Runtime packages have complete license inventory, and 449
production files passed the bounded secret scan with inventory digest
`ce27b97be6d9d70524aad546b1d8e73f5aaf2f8dc75560c128e2686c4d70c0fa`.
Its local report is
`.candidate/quality/supply-chain-local-current-89fab32a.json`, SHA-256
`2df5d8288560ee5d476eb6f447be3e79383379d264940069ac2eacca3260a2db`.
No deployment or user update was attempted.

## 2026-07-14 - Live provider and CDP publication gates made fail-closed

An audit of the protected Candidate workflow found a release-contract gap:
platform, soak, signing and supply-chain receipts were mandatory, but real
managed inference and the requested post-build CDP pass were not members of the
Control Plane's authoritative gate set. The workflow could therefore publish a
correctly signed but unusable product if Model/Image Gateway access was broken.

The release contract now adds `live-model`, `live-image` and `cdp-acceptance`
for both canary and stable. A new post-signing job runs on the protected
`ecorex-live-acceptance` Windows x64 runner, downloads the exact signed
Candidate, invokes a digest-pinned environment driver through a bounded process
boundary, validates one redaction-safe exact-schema result and binds each
execution independently to the authenticated Candidate receipt, release
manifest, platform provenance and current workflow run. The remote publication
job now depends on that job and downloads only the newly uploaded
`ecorex-v1-accepted-*` artifact.

The evidence validator fixes the required product assertions: GPT-5.6 SOL,
medium reasoning and 272,000-token compaction; Image 2 selected through ranked,
non-exclusive routing while read/fetch/vision/CDP/shell remain discoverable;
four concurrent unique live images with zero server errors; rectangle retouch
with a changed revision and at least 0.95 unchanged-region similarity; and 18
real-user CDP scenarios over the four responsive viewports with zero console,
page or request errors. Evidence may contain only bounded metadata and hashes.

The wrapper authenticates the signed release manifest, signed Candidate receipt
and protected staging provenance before the environment driver can inspect or
activate bytes. Its failure channel now emits only controlled error codes or
exception class names, so loader, filesystem and provider paths cannot escape.

The new gate suite passes 13 tests. The affected Candidate, exact-byte
promotion, Control Plane publication/admin and real Web release suites pass 52
tests. The broader release/Candidate/Control Plane selection passes 335 tests
with four explicit platform skips. Python compilation, Ruff, the 635-file
source-tree gate and workflow YAML parsing pass. Current-source supply-chain
preflight passes with 23 locked/licensed Runtime packages and 451 secret-scanned
files; report
`.candidate/quality/supply-chain-local-live-acceptance-gates-final-v3.json`, SHA-256
`f9dced93aead52c7f7eddad61a59d274ef1e6065d70f842ccaf85b63de8132b3`.
No live driver or provider was available in the current shell, so the three new
gates are correctly unresolved and no publication was attempted.

## 2026-07-14 - Candidate-bound gate authority and read-only administrator projection

A second publication-path audit found that the Control Plane still accepted
individual administrator-written `passed` gate rows. That made the database a
parallel pass authority even though CI had produced stronger immutable evidence.
The release contract now has one pass authority: an exact Candidate-bound gate
bundle signed with the same trusted release identity as the manifest. The bundle
binds phase, commit, workflow run, release ID, version, channel, build digest,
manifest digest and the complete expected gate set. Prepare and final stable
bundles are distinct; only a verified final bundle can authorize publication.

The Control Plane stores each attestation once under an immutable database
constraint, materializes all passed gate projections in the same transaction,
and re-verifies the stored final bundle before publication. Manual `passed`
writes are rejected; a conservative manual failure may block an unattested
Candidate, while all manual gate mutations stop after attestation. The CLI now
authenticates the manifest, every artifact, the signed bundle, publication
receipts and Bootstrap index proof as independent trust boundaries.

The Candidate registration contract also persists the SHA-256 of the exact
uploaded `release-manifest.json` bytes separately from its canonical database
JSON digest. The promotion CLI supplies the already authenticated file digest;
the administrator Web computes it from the selected `ArrayBuffer` through Web
Crypto before parsing. Gate-bundle import and final publication both compare the
signed bundle digest with that immutable Candidate fact, closing the remaining
raw-byte/semantic-manifest ambiguity.

The administrator Web surface was reduced to a read-only machine-gate table.
There is no per-gate status selector, evidence field, pass button or browser
bundle-upload path. Two real-browser tests exercise desktop and 390 px layouts,
server-authoritative publish confirmation, reload token clearing and missing-live-
image blocking. The complete Playwright suite now has 36 passing tests, including
the fixed Composer rule: centered only while choosing a new general/project task,
and bottom-anchored in every normal conversation. The Web contract suite has 162
passes. Broad release/Candidate/Control Plane regression has 342 passes, four
explicit platform skips and zero failures. No release or user rollout occurred.

The first complete Python rerun deliberately stopped the batch with 1,877 passes,
17 skips and two failures. One was a stale dependency-lock occurrence count after
adding the signed-gate finalization job. The other exposed a real durable-ordering
defect: multiple Replay user Items created within one coarse Windows clock tick
shared a timestamp, while the random ULID suffix could sort them as revision 2,
3, 1 after restart. The identity authority now emits process-local monotonic ULIDs,
retaining a fresh 80-bit seed for every new millisecond and remaining monotonic
during a wall-clock rollback. Two direct clock tests and 20 independent SQLite/
Runtime restart repetitions pass. The final current-source v1 suite passes 1,881
tests with 17 explicit environment skips and zero failures in 763.48 seconds.

Current-source supply-chain preflight also passes: 23 locked Runtime packages
have complete license inventory, and 454 production files pass the bounded secret
scan with inventory digest
`861351298970c08fb1bb28d55884c32c11acc97e64d0ab2af1fa3a44ccf991bf`.
The Git-admission source-tree gate accepts all 640 authoritative v1 source files.
The ignored local report is
`.candidate/quality/supply-chain-local-signed-gate-bundle-final.json`, SHA-256
`d4e32fd94e15fe2f9ef0444c9bdf78a333a6a6a9b327c8d53fa43a0f6b41e100`.

## 2026-07-14 - Immutable Candidate handoff and resumable administrator publication

The protected-chain audit found that the requested “build/test first, publish
after approval” flow was not actually resumable. Candidate construction and
publication lived in one workflow; a later dispatch rebuilt under a new run ID,
so it could not reuse the exact live-accepted Artifact. A runner loss also lost
the randomly generated Control Plane request journal.

Candidate now stops after `ecorex-v1-accepted-<channel>` and has no origin or
Control Plane credential path. A separate protected publication workflow
requires the exact Candidate run ID, attempt and Artifact ID; validates the
successful protected-main workflow, repository/head repository, commit,
timestamps, non-expiration, unique accepted name and archive digest; then
re-authenticates the signed Candidate and complete gate set before approval.
The default is `verify-only`. `create` and `create-and-activate` are the only
remote mutation modes.

Cross-workflow downloads no longer rely on a digest warning. Both the selected
accepted Artifact and the verified same-run publication input are fetched by
exact Artifact ID, SHA-256 checked and extracted by a bounded ZIP reader that
rejects traversal, Windows path aliases, links/special files, case collisions,
duplicate members, unexpected roots, excessive size/member counts and
insufficient disk. Evidence assembly additionally requires the exact original
Candidate workflow run ID.

Promotion request IDs are now deterministic across lost journals and workflow
reruns while remaining bound to the release, exact manifest, publication
receipt, rollout target, preparation evidence and operation. The focused
handoff, workflow, live-gate, promotion and Control Plane regression passes 57
tests. The broader release/Candidate/Control Plane/update/public-Bootstrap/live
selection passes 474 tests with seven explicit platform skips and 1,431
deselected. Python compile/Ruff, workflow YAML parsing and `git diff --check`
pass. Current supply-chain preflight passes 23 locked/licensed Runtime packages
and 459 production files, inventory SHA-256
`2f8f67bacb8439bfbbc32b0a9d4e52c737e329447220758a349077e6f23ee05f`;
the 21,857-byte ignored report SHA-256 is
`8756db7125a8fe020743b9b02e2ed2ac17c7eb3fcd5307a3704946cd937b673e`.
The Git-admission gate accepts 646 source files. No protected Candidate was
available and no publication, deployment or user rollout was attempted.

## 2026-07-14 - Split-workflow dependency contract and complete convergence rerun

The first complete rerun after separating Candidate acceptance from publication
produced 1,894 passes, 17 explicit skips and one failure. The failure was kept as
a release-chain defect: the dependency-lock checker still described the former
combined workflow and required five Runtime-profile installations from the
Candidate alone. It therefore rejected the intentional three/two split between
Candidate construction and protected publication.

The workflow dependency gate now uses an explicit per-workflow capability
contract. Candidate requires three locked Runtime installs plus its fixed
dev/cloud and Node/npm build inputs. Publication independently requires exactly
two locked Runtime installs and rejects any Node/npm install because it consumes
already-built immutable Web bytes. CI and platform-stage retain their own exact
profiles and toolchain requirements; floating pip/npm installs and unpinned
Actions remain rejected.

The corrected dependency check reports 23 locked Runtime packages and 282 npm
packages. The affected publication/Candidate suite passes 59 tests. The complete
current-source Python v1 rerun passes 1,895 tests with 17 explicit environment
skips and zero failures in 758.93 seconds. Ruff, Python compilation,
dependency-lock validation, the 646-file source-tree gate and `git diff --check`
pass. Supply-chain preflight passes 23 locked/licensed Runtime packages and 459
production files, inventory SHA-256
`2f169bf36d5d6eb509d1dafa25383fa44589f9930e0ba6e2b8e6054b734c9540`;
the ignored 21,857-byte report is
`.candidate/quality/supply-chain-local-candidate-handoff-contract-fix.json`,
SHA-256
`ac3ac6f993e3f664af7c214094b2b7b2c49997e37b40bb94223ff795bdc9b3a8`.
No Candidate was dispatched and no origin, Control Plane or user rollout was
mutated.

## 2026-07-14 - GitHub release repository readiness becomes machine-verifiable

A read-only audit of the actual private repository found that local source
readiness and remote release readiness had diverged. `main` remained at
`b52999b07a753e103a993a4da9d3c83c3f366e71`, the v1 branch was not current on
the remote, only four legacy workflows were active, branch protection and
protected Environments were absent, and no self-hosted Runner, Actions variable
or Secret name was registered. The repository allowed all Actions. The active
administrator OAuth identity had `repo`, `read:org` and `gist`, but not
`workflow`; GitHub rejected the attempted branch push before changing any ref.

Added separate backend repository-readiness contract/evaluator and bounded
GitHub administration transport modules plus an administrator CLI, avoiding a
new mixed-responsibility release monolith. The contract covers the four exact
v1 workflows, five required CI status contexts,
strict PR/admin/linear-history protection, GitHub-owned-only Actions with
read-only default workflow permissions, six protected Environments, every
required variable/Secret name and seven online Runner roles. Signing, live
acceptance and publication roles must not resolve to the same Runner identity.
The report never requests or contains Secret values.

`audit` is read-only. `bootstrap` requires the exact repository twice, an exact
40-character default-branch head and a resolved reviewer before applying only
idempotent Environment, Actions-policy and branch-protection PUTs. A changed
head fails before any write. Runner registration and configuration values remain
external facts, so governance creation cannot manufacture a green result.

The live audit exits 2 with 22 stable blockers: three Actions-policy findings,
one unprotected branch, one missing OAuth workflow scope, six Environments,
seven Runner roles and four inactive v1 workflows. Evidence is
`.candidate/quality/github-release-readiness-live-v2.json`, 3,621 bytes,
SHA-256 `a81a2f26bc4b7f674441ee340b17a368bb0478b40fec9ae69996aa7cefc0c15e`.
Nine direct governance tests and the 51-test affected release/Candidate/package
selection pass; Ruff, Python compilation, dependency locks, the 650-file source
gate and diff checks pass. The complete current-source v1 suite passes 1,904
tests with 17 explicit environment skips and zero failures in 753.89 seconds.
Supply-chain preflight passes 23 locked/licensed
Runtime packages and 462 production files, inventory SHA-256
`46f9c75f4517e47103249cbf27a65b21e3ed2933dae8aa9800d410849ed32210`;
the ignored 21,857-byte report is
`.candidate/quality/supply-chain-local-github-readiness-split.json`, SHA-256
`573c9141dff494b6ffe7f7a212ccedfe4ee66a530d1d5e3f547225f1d55507a8`.
No repository setting, ref, workflow, release or user rollout was changed.

## 2026-07-14 - Non-privileged release capacity moves to ephemeral hosted runners

The prior repository contract correctly isolated signing, live provider/CDP
acceptance and origin publication, but also required four permanent machines for
jobs that receive none of those privileges: Windows x64 stage, macOS arm64/x64
stage and the four-hour image soak. For a personal/small-team Web product this
made the release fleet heavier than the product and left seven Runner blockers
in an otherwise deterministic chain.

macOS platform stage now uses the fixed GitHub-hosted labels `macos-15` and
`macos-15-intel`. The real PostgreSQL 16.9/MinIO soak uses a fresh
`ubuntu-24.04` VM for its required 14,400 seconds, inside GitHub's documented
six-hour job limit. Protected Environments and the immutable workflow/commit/
receipt contract remain unchanged. Only external signing, persistent Windows
live Model/Image/CDP acceptance and publication remain distinct self-hosted
roles.

Hosted jobs cannot consume a host-local Runtime config path, so the public
production config now crosses the protected Environment boundary as canonical
Base64 plus an independent SHA-256. The materializer enforces GitHub's 48 KiB
single-variable limit (36 KiB decoded), strict Base64/UTF-8/JSON, duplicate-key
rejection, required identity shape, exclusive file creation, stable file
identity hashing and digest-fenced cleanup. It writes only a redacted receipt;
the stager repeats its full production schema and secret scan. Credentials and
managed model keys remain forbidden from this public config.

The exact provider-boundary, digest, duplicate-key, conflict/alias, cleanup and
workflow/Runner contracts pass 32 focused tests. The affected platform,
Candidate, dependency and package selection passes 103 tests with one explicit
platform skip. The complete current-source v1 suite passes 1,915 tests with 17
explicit environment skips and zero failures in 758.22 seconds. Full Ruff over
`ecorex`, `scripts` and `tests/v1`, Python compilation, workflow YAML parsing,
dependency locks, `git diff --check` and the 653-file Git-admission gate pass;
five pre-existing dynamic-path import annotations in three old scripts were
made explicit so the full Ruff claim no longer depends on a narrowed target.

Current supply-chain preflight passes 23 locked/licensed Runtime packages, 282
npm packages and 464 production files, inventory SHA-256
`5515c74a6183ad1bdd9f279f8c77441ba750799315eaf70f8d56c0c42389bf58`.
The ignored 21,857-byte report is
`.candidate/quality/supply-chain-local-hosted-release-runners-final.json`, SHA-256
`5ace12cb9481bb48543f1fd92f8a73003683db0d88b2670a94d9ed2f75a8a54e`.

A fresh read-only audit of the actual private repository exits 2 with 18
blockers: Actions policy 3, branch protection 1, missing OAuth `workflow` scope
1, Environments 6, privileged Runners 3 and inactive v1 workflows 4. Evidence
is `.candidate/quality/github-release-readiness-hosted-runners.json`, 3,226
bytes, SHA-256
`c8d7d0f323e5d7e079ecfd884fd102707348295b917fe2edabb87946bd48a114`.
No repository setting, ref, workflow dispatch, provider session, release origin,
Control Plane state or user update was changed.

## 2026-07-14 - First hosted CI matrix closes hermetic and cross-platform gaps

OAuth workflow authorization was restored, branch
`codex/ecorex-v0.3.0-hardening` was pushed and draft PR #2 created. The first
real `EcoreX v1 CI` run (`29292576944`, commit `f772d0c1`) proved both macOS
targets, then failed the Ubuntu quality job in 13 tests and the Windows x64
smoke in 27 tests. No failed test was waived. The first Windows diagnostic
showed that the builder needed to support both standard VS 2022 Program Files
roots. The Ubuntu
failures exposed an inactive Windows-only lock marker, Web dependencies being
installed after a Python test that performs the real Vite build, and tests
that accidentally used Windows/macOS identities or a symlinked interpreter
from the host instead of the staged Candidate identity.

The native builder now searches both standard SpecialFolder roots, deduplicates
matches and still accepts exactly one compiler whose complete toolchain identity
matches the reviewed manifest. CI installs the npm lock before the Runtime
suite. The license gate remains fail-closed for unknown packages but carries a
reviewed BSD-3-Clause fallback for locked `colorama==0.4.6`, which is inactive
off Windows. Candidate, activation, sandbox and platform-stager tests now bind
their expectations to the Candidate or actual host boundary instead of
fabricating a platform.

Two real Runtime issues found by the Linux run were also fixed. Credential
quarantine link/reparse rejection is normalized to the public
`QuarantineStateError`. POSIX output roots retain a Runtime-owned directory
descriptor so an unlinked inode cannot be immediately recycled and mistaken
for the frozen output policy; descriptors close during Runtime shutdown but
remain available across account logout so local output functions continue.

The 13 original failure cases pass locally (12 passed and one privilege skip),
the affected ten-module regression passes 171 tests with five explicit platform
skips, and the complete v1 suite passes 1,916 tests with 17 explicit skips and
zero failures in 777.01 seconds. npm audit reports zero vulnerabilities;
TypeScript, 162 Web contract tests and the production content-addressed build
pass. Ruff, Python compilation, workflow YAML, design/legacy/download,
dependency-lock, Runtime/Server schema-authority, reproducibility and all 653
source-file gates pass. Supply-chain preflight covers 23 Runtime and 282 npm
packages plus 464 production files, inventory SHA-256
`e3698863ef20ac363a0bca2b89061ed51a7cbe4740e2dcf3079b59e54888fac1`.
The ignored 21,857-byte report is `.ci/ci-fix-supply-chain.json`, SHA-256
`3985d06e199b964be74f289d3e3b6f29a018c8a0f0de39196d6c1d9762093ef0`.
The corrected remote matrix has not yet been claimed; it must pass after this
change is pushed before protected release workflows are considered.

## 2026-07-14 - Windows hosted image is bound to the reviewed compiler family

The second hosted run (`29294544893`, commit `7411c561`) passed the Ubuntu
quality job and both macOS architectures. Windows again produced 27
native-build cascades, now with the precise
`trusted_visual_studio_layout_unavailable` boundary. GitHub's official runner
image migration record confirms that `windows-latest` and `windows-2025` moved
to Visual Studio 2026 in June 2026; those images no longer provide the VS 2022
layout required by the reviewed EcoreX manifest.

Read-only CI now selects fixed `windows-2022`, matching the MSVC 14.44/19.44 and
Windows SDK 10.0.26100.0 family. The two-root discovery correction remains, and
exact file digests, Authenticode, libraries, reparse rejection and the
one-toolchain rule remain fail-closed in release mode. A v143 compatibility
component on VS 2026 is not treated as equivalent without a future manifest
review and deterministic rebuild evidence.

The runner/workflow contract passes 19 focused tests. Workflow YAML, progress
JSON, dependency locks, the 653-file admission gate, reproducibility and
`git diff --check` pass. Current-source supply-chain preflight covers 23
Runtime packages, 282 npm packages and 464 production files, inventory SHA-256
`5132465547eb7323d6fb2b9c7a481c170a1648c9fe4229b4b006ca6faf21d068`.
The ignored 21,857-byte report is
`.ci/windows-runner-contract-supply-chain.json`, SHA-256
`716078b019b6c5b0ef21abf252fd1343d256e1e8b25ecaad25f4e601e6057c9a`.

## 2026-07-14 - Mutable hosted compiler is separated from Candidate authority

The third hosted run (`29294972413`, commit `ba595b5b`) confirmed the fixed
`windows-2022` image and VS 2022 layout, but failed at
`trusted_msvc_layout_unavailable`: its weekly image carries a different
`cl.exe` digest from the exact locally reviewed manifest. This is not repaired
by updating the hash to whichever weekly image happens to run. A GitHub OS
label is an image family, not an immutable toolchain identity.

The Windows builder now has two non-interchangeable modes. Default/release mode
is unchanged: caller-pinned manifest and source digests, exact tool/library file
hashes, exact versions and certificate thumbprints, locked files and a
`caller-pinned` receipt. The opt-in compatibility mode is accepted only with an
explicit switch inside GitHub Actions on Windows `win22` for pull request, push
or manual CI. It still fixes the MSVC 14.44 family and SDK version/layout,
requires valid Microsoft Authenticode, rejects links/injection, locks every
tool/library and records observed hashes, but emits
`github-hosted-ci-compatibility`. The production platform stager rejects that
authority mode.

Read-only CI alone sets the compatibility request. Protected Windows staging
now targets `[self-hosted, windows, x64, ecorex-platform-windows]`; macOS remains
on hosted fixed labels. Repository readiness requires the Windows build Runner
as a fourth isolated role and rejects any physical Runner overlap with signing,
live Model/Image/CDP acceptance or publication. The build host receives none of
those privileged credentials.

Local default exact-mode execution passed 90 tests with two explicit platform
skips; the one initial failure was a stale static string assertion. The corrected
workflow/stager/native contracts pass 57 tests with one skip. A separately
simulated GitHub `win22` compatibility invocation compiled the real native
helpers and passed the canonical Runtime probe. Full Ruff/compile validation
passes. The complete current-source v1 suite passes 1,916 tests with 17 explicit
environment/platform skips and zero failures in 761.34 seconds; five warnings
are unchanged upstream Starlette/websockets deprecations. Repository governance
also uses the real `Windows x64 compatibility` Job context rather than the stale
short name, preventing an impossible branch-protection requirement. Remote CI
must still revalidate the exact new commit before promotion.

Final current-source supply-chain preflight passes 23 locked/licensed Runtime
packages, 282 npm packages and 464 production files, inventory SHA-256
`450831462f495c7d522af97f5ebd6cf7353e7e2ce1ed1b9aa041cd9d8e4b8528`.
The ignored 21,857-byte report is
`.ci/windows-ci-release-separation-supply-chain.json`, SHA-256
`a087fbc2a34a26e67b4f15c08dcbc426c7c01f00457db9d102748218e43887ad`.

A fresh read-only audit of the actual private repository exits 2 with 17
blockers: Actions policy 3, branch protection 1, Environments 6, isolated
Runners 4 and inactive protected workflows 3. OAuth now includes `workflow`,
and the v1 CI workflow is active, so those two former blockers are closed. The
new `platform-windows` Runner appears explicitly. Evidence is
`.ci/github-release-readiness-dual-mode.json`, 3,167 bytes, SHA-256
`d9eb1f478307b94f418de7f855be36700fd140e294b8ab4693f10cd01338a2c8`.
No governance mutation was performed.

The fourth hosted run (`29296280821`, commit `afcb166b`) passed Ubuntu quality
and both macOS architectures, and proved the new Windows boundary: all 150
platform-sensitive Runtime/native tests passed on the hosted VS 2022 image. The
Job then failed in Web `contracts:check`, before TypeScript
compilation, because Git checkout converted the generated `.ts` contract to
CRLF while the deterministic generator emits LF. JSON/JS/CSS and other release
text already had explicit `.gitattributes` policy; TypeScript was the missing
family. `*.ts` and `*.tsx` are now globally fixed to `text eol=lf`, and the
reproducibility gate plus unit contract require both lines. The generated Runtime
contract, byte gate, eight focused tests and full Web typecheck pass locally.
No generated schema or UI behavior changed.

Current-source supply-chain preflight remains green for 23 Runtime packages,
282 npm packages and 464 production files, inventory SHA-256
`cfb99101bfa3b182222790525a4597c5eb432ebf99d183e38ce08a4ded0d0e00`.
The ignored 21,857-byte report is `.ci/typescript-eol-supply-chain.json`,
SHA-256
`9084448e1b414a3a734139493f48426053c84ca746e982f24800884ba2a157a2`.

The fifth hosted run (`29296609455`) completed successfully on exact commit
`a70d65c3105d9156bce21fea98eeddb779ba4c90`. Ubuntu quality, Windows x64,
macOS arm64, macOS x64 and the final four-contract byte comparison all passed.
Windows crossed the previous generated-contract failure, then completed
TypeScript, the content-addressed Web build and byte upload. This is read-only
CI evidence; it does not claim protected platform-stage, signing, live provider
acceptance, publication or user rollout.

The final Draft PR head `a11dbd884054130ecec145c0a2625ec4eb2c4cca` was then
revalidated by hosted run `29296947260`; Ubuntu quality, Windows x64, macOS
arm64/x64 and cross-runner byte stability all completed successfully.

## 2026-07-14 - Live upstream recovery and final WebUI browser rerun

The previously unavailable administrator upstream recovered. A bounded,
redaction-safe local diagnostic confirmed that its model catalog now contains
`gpt-5.6-sol`; one medium-reasoning Responses request completed with HTTP 200
under the frozen 272,000-token policy. A single Image 2 admission completed
before load was increased. Four no-retry requests were then admitted with a
hard worker limit of four: all four completed, all four content digests were
unique and no 5xx response occurred.

One real rectangle/mask edit changed the selected mug region while retaining
`0.991565` mean non-target similarity. Visual inspection confirmed the intended
localized colour edit. The source image, mask and result remain ignored local
quality artifacts; the tracked evidence contains only byte counts, durations,
digests and normalized quality metrics.

The final content-addressed Web build was rerun through the in-app browser at
1440x900. The browser confirmed model selection before the first message,
independent Image 2 mode, bottom-anchored normal Composer, continuous reasoning
replacement, fit-first full image preview, exact structured-retouch summary,
output-location mutation, memory reset/undo, persistent Full Access with
one-click revoke, unified extension management, formal Feishu/Tencent Docs
entries and task-ID continuation. The real Control Plane share renderer was
also exercised on loopback; it preserved user/Agent role order and served a
complete 1800x1100 image. Main and share tabs emitted zero console warnings or
errors.

This closes the former direct-upstream availability diagnostic only. It does
not create an official `live-model`, `live-image` or `cdp-acceptance` receipt:
the probe used the existing local administrator policy rather than the
protected device-flow session, and the WebUI used a loopback Runtime fixture.
The immutable Candidate, managed Gateway, protected runner, repository
governance and publication readback remain mandatory. Evidence:
`evidence/live-provider-local-diagnostic-2026-07-14.json`.

## 2026-07-14 - Core Thread/Turn projections become generated fail-closed contracts

The Runtime/Web boundary still contained one architectural exception to the
backend-authoritative rule. The Python kernel returned typed Thread, Turn,
Item, Job and Interaction projections, but eleven critical FastAPI routes did
not declare response models. The Web client then accepted those bodies through
compile-time casts; its handwritten types also omitted the durable `inherited`
facts carried by Turn and Item projections. A stale or cross-thread response
could therefore enter the reducer before contract drift was detected.

All create/rename/archive/restore/fork Thread routes, create/steer/queue/
replace/interrupt Turn routes and the complete Thread projection route now
declare their exact Pydantic response model. OpenAPI assertions pin the status
code and component schema for every route. The deterministic contract generator
now includes Thread/List/Turn/Item/Job/Interaction, mutation, replace and full
projection schemas plus their Runtime enums. TypeScript consumes the generated
status/kind unions and includes both `inherited` fields.

The Web transport validates every affected response before reducer state. It
rejects missing or extra wire fields, unknown state-machine values, malformed
timestamps, Thread identity contamination, Job/Turn mismatches and inconsistent
replace identities. Projection validation is a self-contained dynamic boundary:
the initial Runtime client remains below its fixed budget, while the strict
11.09 KiB projection validator loads only when these endpoints are used. A
shared branded error preserves one user-facing incompatibility path across the
deferred module without creating a content-addressed dependency cycle.

Focused Runtime/OpenAPI integration passes 44 tests. The complete Python v1
suite passes 1,916 tests with 17 explicit environment/platform skips and zero
failures in 831.79 seconds. TypeScript, all 163 Web contract tests and the
production build pass. The build contains 19 content-addressed assets / 18 JS
chunks; initial JavaScript is 474.65 KiB (147.02 KiB gzip), within the unchanged
475 KiB hard limit. Ruff, Python compilation, design, legacy, public-download,
dependency-lock, Runtime/Server schema authority, diff and the 655-file source
admission gates pass.

The current-source supply-chain preflight also passes: 23 locked Runtime and
282 npm packages are license-accounted, and 466 production files pass the
bounded secret scan. Inventory SHA-256 is
`669aa0f4d23ef9aca2b7eb91e785c70438a3d315d0726756cd723b2198fe8394`.
The ignored 21,857-byte report is
`.ci/projection-contract-supply-chain.json`, SHA-256
`f50da3a5b4412c547c1c35f0c4dea6a956f6f5b3e4485140c3c77c660f56857a`.

The source-bearing commit `6d4c3030717ce078a6d5a74b830ec9a169a32d2e`
is independently green in hosted run `29301500258`. Ubuntu quality executed
the complete Runtime, npm audit, TypeScript, all Web tests, the production
build and static product gates; Windows x64 and macOS arm64/x64 compatibility
jobs produced their byte contracts; the final four-runner comparison accepted
identical canonical bytes. This evidence update changes documentation only.
No protected Candidate, repository-governance mutation, publication or user
rollout was performed.

## 2026-07-15 - Connector-login HITL becomes a typed, snapshot-consistent boundary

The remaining connector-login lifecycle was still an exception to the
backend-authoritative Runtime contract. Begin, check and cancel returned
untyped dictionaries, their `202` polling shape was absent from OpenAPI, and
the Web transport trusted compile-time casts. More importantly, the completed
check replay assembled Interaction, Turn, internal DurableJob and event
watermark through separate reads. Besides allowing a cross-snapshot response,
that path serialized Runtime-only lease, checkpoint, idempotency, payload and
raw-error fields into the public response.

Begin/check/cancel now have exhaustive Pydantic response models. Connected,
awaiting-callback and retry-required states are mutually constrained; both the
`200` and `202` OpenAPI responses point to the same complete polling contract.
Nested Interaction, Connector, Thread, Turn and Job identities are checked in
Python before a response can leave Runtime. Completed replay now calls one
kernel projection method that reads all related rows and the watermark inside
one SQLite reader transaction. DurableJob is reduced by the existing
secret-free JobProjection and its thirteen-field allowlist; no internal task
field is serialized.

The deterministic Python-to-JSON-Schema/TypeScript generator now includes the
three lifecycle responses and InteractionMutationResponse under schema digest
`310063327c32d3ae9101ef2565ce020c668cc900f77971c262c5398f8d12195d`.
The Web Runtime client dynamically imports one shared lifecycle validator and
rejects absent/extra fields, impossible state variants, requested-ID drift,
Connector drift and nested Thread/Turn/Job contamination before reducer state.
Keeping this work in the existing deferred projection chunk preserved the
initial Web budget.

The first complete run correctly found one pre-existing replay leak: the
response model rejected the raw DurableJob and returned 500 on a second status
check. The fix was made at the kernel projection boundary, not hidden by
loosening the model. The exact failed test then passed, 58 focused
Runtime/Connector/discovery tests passed, and a fresh complete run passed 1,916
tests with 17 explicit environment/platform skips and zero failures in 818.30
seconds. TypeScript, all 164 Web tests and the production build pass. The build
contains 19 content-addressed assets / 18 chunks; initial JavaScript is 474.84
KiB (147.05 KiB gzip), the deferred feature total is 94.42 KiB (33.67 KiB
gzip), and the strict projection chunk is 15.40 KiB (4.04 KiB gzip).

Ruff, Python compilation, generated-contract freshness, design, legacy,
public-download, dependency-lock, Runtime/Server schema authority,
reproducibility, diff and all 655 admitted source files pass. npm audit reports
zero vulnerabilities. Supply-chain preflight covers 23 locked/licensed Runtime
packages, 282 npm packages and 466 production files; its inventory SHA-256 is
`33048f7804b73a07ff3082605d07f1b383016b0b6174517730242db4db393dfc`.
The ignored 21,857-byte report is
`.ci/connector-login-boundary-supply-chain.json`, SHA-256
`355cbb87ca9bfc30cda141b9998c347fadbf67225b612a07f7fbee1ebc6f4d31`.

The source and local-evidence commit
`3b9d684a311828d913f3c29c626f6b68f4e6cd95` is independently green in hosted
run `29357245885`. Ubuntu quality, Windows x64, macOS arm64, macOS x64 and the
final cross-runner byte comparison all completed successfully. Draft PR #2 is
still CLEAN and intentionally remains a Draft.

A fresh read-only repository audit remains byte-for-byte identical to the
previous receipt: 17 blockers, comprising Actions policy 3, main protection 1,
six Environments, four isolated Runner roles and three protected workflows not
active on main. The 3,167-byte ignored report is
`.ci/github-release-readiness-connector-boundary.json`, SHA-256
`d9eb1f478307b94f418de7f855be36700fd140e294b8ab4693f10cd01338a2c8`.
No governance setting, protected Candidate, managed provider/CDP gate, release,
rollout or user installation was changed.

## 2026-07-15 - GitHub Actions moves to one reviewed Node 24 supply-chain authority

GitHub had begun forcing the old checkout/setup/upload/download revisions from
their declared Node 20 runtime onto Node 24. The workflows still executed, but
the warning exposed three structural gaps: the dependency gate accepted any
40-character Action SHA instead of one reviewed revision; two inherited
CowAgent Docker publishers remained discoverable even though their repository
guard made them dead here; and two CI checkouts had not explicitly disabled
credential persistence.

All four v1 workflows now use verified official Node 24 revisions: checkout
v7.0.0, setup-python v6.3.0, setup-node v7.0.0, setup-go v6.4.0,
upload-artifact v7.0.1 and download-artifact v8.0.1. Full commit SHAs remain in
workflow source. `requirements/locks/github-actions.json` is the declarative
review authority for repository, release, commit, runtime, verification and
release URL. Its SHA-256 is
`4c6d80f57f3c6a178b611776eadb339cf80c45418ded644b2f7272f6144b5a97`.
The lock fixes the minimum protected self-hosted Actions Runner at 2.327.1.

The dependency gate now inventories every `.yml` and `.yaml` workflow, permits
only the four v1 workflow contracts, requires an exact lock match for every
`uses:` line, and requires `persist-credentials: false` after every checkout.
The Action lock is also part of the cross-runner byte contract and source-tree
admission. The two CowAgent Docker workflows were deleted and added to the
permanent legacy cutoff so they cannot be restored as a side channel.

The first complete local regression was informative rather than green: 1,921
tests passed and one shutdown-isolation test measured the outer process wall at
the duplicated strict `3.5 < 3.5` boundary. The actual child-reported shutdown
and default-executor hard budgets remained below 0.8 seconds. The root cause was
the test coupling variable Windows cold-import/process scheduling to the
functional shutdown deadline while `subprocess.run(timeout=4)` already owned
the independent process-exit deadline. The duplicate wall assertion was
removed without changing either production timeout. The exact test then passed
five consecutive runs, including loaded-host runs whose overall pytest time
varied substantially.

The final exact-source regression passes 1,922 tests with 17 explicit
environment/platform skips and zero failures in 1,255.82 seconds. Its ignored
JUnit is 384,624 bytes, SHA-256
`2ab35f024961fa565a0552c934325c6ca5810c580ec86c735547c602a51bbd5c`.
Ruff/compile, 49 focused release/dependency tests, YAML parsing, all static
gates and 656 admitted source files pass. Web audit has zero vulnerabilities;
TypeScript, all 164 Web tests and the 19-asset/18-chunk production build pass at
474.84 KiB raw / 147.05 KiB gzip initial JavaScript.

The final byte contract is 7,173 bytes, SHA-256
`a6cc2b6cb49eb0ab671818fdad2d074783adcf2cd1a610fa16bf576914a64b36`.
Supply-chain preflight covers 23 Runtime packages, 282 npm packages and 467
production files with inventory
`e488a5e95fd80a5594d89cd2cdfb967f5d0d74f82a3d51ca77dcc3710db67edb`;
the ignored 21,857-byte report SHA-256 is
`0a0dc45a87435a7b6fbfce47ec2b7768e53366643724ed24779acdcf62ea49dc`.
Hosted execution and the live governance audit remain separate evidence and
are not claimed by this local checkpoint.

Source commit `fd05f42413b2563e34f15421e58991248f3bdee2` is now independently
green in hosted run `29382330122`. Ubuntu quality, Windows x64, macOS arm64,
macOS x64 and final cross-runner byte stability all completed successfully.
The five check runs contain zero annotations, and the complete hosted log has
no Node 20 forced-runtime or deprecated-Action warning. This proves the locked
Node 24 Action closure executes on all current hosted platforms, including the
v8 artifact download used by the final byte comparison.

Draft PR #2 is CLEAN, MERGEABLE and remains intentionally Draft. The fresh
read-only governance audit is byte-identical to the prior receipt: 17 blockers
(Actions policy 3, main protection 1, Environments 6, isolated Runner roles 4
and protected workflows inactive on main 3). The 3,167-byte ignored report
SHA-256 remains
`d9eb1f478307b94f418de7f855be36700fd140e294b8ab4693f10cd01338a2c8`.
The deleted CowAgent workflows therefore remain active on the current main
branch until a reviewed merge; no protected Candidate, publication or rollout
was attempted.

## 2026-07-15 - secondary Runtime JSON boundaries become generated contracts

Memory, Output, legacy migration quarantine and System observability exposed a
second thin-frontend gap. Their domain services owned the facts, but twelve
HTTP routes returned dictionaries and the Web client asserted response types.
Migration validation had also leaked into `SettingsDialog`. This meant the
server could emit an additional field, stale aggregate count, contradictory
lifecycle or cross-Artifact materialization identity before React noticed.

Nine strict Pydantic response models now cover all twelve routes. They reject
extra fields and validate timestamp, count, digest, ID, lifecycle and aggregate
invariants before the response leaves FastAPI. Migration deletion deliberately
retains aggregate category facts for audit; the old component validator that
incorrectly rejected those retained facts was removed. System technical
metrics retain exactly `runtime`, `process`, `storage` and `services` roots and
use bounded recursive JSON depth/cardinality/string/key/finite-number rules.
Output locations expose only the three product aliases and never a host path.

The generator now emits a 36-contract canonical schema plus a dedicated
`generatedSettingsRuntimeContract.ts`; schema SHA-256 is
`877c962e459088e8ddc0f833a50f568cad6418fb0d9a14b904019028654b0d50`.
`RuntimeClient` dynamically loads one settings validator before admitting any
of the nine response families to state. A locally branded contract error keeps
the deferred module independent of the client while preserving the shared
error identity. Component-level migration validation was deleted.

The first eager implementation made the real release build fail at 485.73 KiB
against the unchanged 475 KiB initial-JavaScript limit. The first lazy attempt
then correctly failed the content-addressed dependency graph because settings
validation imported the client in reverse. Splitting the generated settings
manifest and error brand removed both root causes. The final build contains 20
assets and 19 JavaScript chunks: entry 47.90 KiB raw / 14.40 KiB gzip, initial
JavaScript 474.99 KiB / 147.12 KiB gzip, deferred features 93.28 KiB / 33.23
KiB gzip, and settings validation 11.73 KiB / 3.59 KiB gzip.

Two existing wall-clock tests then exposed host-load coupling during the final
full runs. A connector shutdown child included cold import and SQLite bootstrap
inside its four-second process-exit window; it passed 4/5 repeats despite the
child shutdown itself remaining bounded. A ready/start handshake now begins
the four-second guard only after setup and passed 10/10 repeats. A maintenance
test slept 40 ms and guessed that two cycles had run; under load it observed
one. It now waits for the actual second-call Event under a two-second failure
deadline and passed 10/10 repeats. Product shutdown and maintenance intervals
were not relaxed. The combined connector/shutdown files pass 26 tests.

The final current-source suite passes 1,926 tests with 17 explicit environment
or platform skips and zero failures. JUnit records 1,943 cases in 1,680.264
seconds, is 385,219 bytes, and has SHA-256
`dc050061ff70766a1ef0379cc12879d8d46372024534452891bcd3778e54f2db`.
Ruff/compile, npm audit, TypeScript, 167 Web tests, generated-contract freshness,
design, legacy, public-download, dependency-lock, Runtime/Server schema,
reproducibility, diff and all 658 admitted source files pass.

Supply-chain preflight accounts for 23 Runtime and 282 npm packages and scans
468 production files. Its inventory is
`a7b2ff6ff3d468344a20f657f421344630e49ac798d5aa8912c70276f31a8d5a`;
the ignored 21,857-byte report SHA-256 is
`a742ef60ce397ee547f74ba508e0f730725fb068cb44b6f28fd0ae27a3862282`.
The 7,380-byte byte contract SHA-256 is
`65eb5c816f13f473f70f3084ecc07e4c9bd1c3febda02429672735b081ff3ec5`.
Hosted CI and live governance remain separate post-push gates; no Candidate,
publication or rollout is claimed by this local checkpoint.

Source commit `ee8a7f8cc77830b66358af3acc9206f95cb5923b` is now independently
green in hosted run `29390253811`. Ubuntu quality, Windows x64, macOS arm64,
macOS x64 and Cross-runner byte stability all succeeded. All five checks have
zero annotations, and the complete log has zero Node 20 forced-runtime or
deprecated-Action warnings. Draft PR #2 is CLEAN, MERGEABLE and remains Draft
at the exact source head.

The post-push repository audit is byte-identical to prior evidence: 17 blockers
across Actions policy 3, main protection 1, Environments 6, isolated Runner
roles 4 and inactive protected workflows 3. Its ignored 3,167-byte report
SHA-256 remains
`d9eb1f478307b94f418de7f855be36700fd140e294b8ab4693f10cd01338a2c8`.
The audit action is `none`; no governance mutation, protected Candidate,
publication, rollout or user update occurred.

## 2026-07-15 - Artifact responses become fail-closed and Workbench surfaces match the supplied swatches

Artifact and precise-retouch routes still had one thin-front violation: domain
services returned correct dataclasses, but eleven JSON endpoints serialized
dictionaries without an ASGI response contract. React then trusted TypeScript
assertions for feedback, external actions, workspace state and Retouch Jobs.
An internal family, extra storage field, stale revision, malformed geometry or
contradictory completed Job could therefore reach the browser before failing.

Six strict public response families now own the boundary: Artifact projection
and list, feedback, external action, Retouch Job and Retouch workspace. Nested
lineage, rendition, quality evidence, request, edit surface, mask, reference,
view state and inspection-region objects reject extra fields. Cross-field
validators enforce public family/visibility, count and identity uniqueness,
digest/timestamp ordering, normalized geometry and mask bounds, request/base
revision, Job/result lifecycle and workspace/reference/result URL identity.
Five binary endpoints explicitly publish `Response` and no JSON response
model. A response-validation failure is normalized by the existing stable API
error boundary and cannot disclose the internal Artifact identity or name.

The canonical generated schema now contains 42 contracts at SHA-256
`5face1daf57ea1c63fd9632143528802014d5ffd2880eb3d46b1f566bea3f12b`.
Artifact field manifests are emitted to a dedicated generated module and are
loaded only when an Artifact operation is used. The first production build
correctly rejected a reverse dependency cycle between the deferred validator
and the initial Runtime client. `runtimeContract` plus its generated manifest
now form an explicit shared contract-core chunk. Artifact request construction
and response validation moved together behind one delayed operation boundary,
restoring a one-way graph and reducing initial JavaScript instead of raising
the release budget. Brush width remains optional, matching the Python domain
default; a regression test covers the previously mismatched no-width form.

The supplied colour crops were measured rather than visually approximated.
Semantic surface tokens now map light non-chat/chat/current/scrollbar to
`#f7f7f7/#ffffff/#ebebeb/#e5e5e5` and dark to
`#0f0f0f/#111111/#202020/#202020`. Light Composer uses the chat surface;
dark Composer, current conversation and scrollbar use the same session
emphasis surface. Sidebar, workspace, timeline, header and Composer consume
those roles; no component gained a raw colour. Forced-colour and contrast
contracts were extended, and the locked `design.md`/Hallmark preflight record
the measured mapping.

Current-source verification is focused and explicit: Ruff passes; 149
Artifact/Retouch Python tests pass with one unchanged upstream warning;
TypeScript and all 176 Web contract tests pass. The content-addressed build
emits 24 assets / 23 JavaScript chunks and passes the unchanged 475 KiB limit
at 474.22 KiB raw / 147.29 KiB gzip initial JavaScript; delayed Artifact
operations are 18.36 KiB raw / 4.68 KiB gzip. Chromium E2E passes 36/36 across
1440x900, 1024x768, 768x900, 390x844 and 320x568 in both themes with zero axe
violations. The same run covers sparse control framing, normal/new Composer
placement, task continuation, reasoning replacement, HITL, sharing, fit-first
image preview and touch Artifact actions. Captured pixels confirm the requested
desktop non-chat and chat swatches exactly.

The earlier complete 1,926-test and five-platform hosted evidence remains
bound to commit `ee8a7f8cc77830b66358af3acc9206f95cb5923b`; it is not relabelled as
exact evidence for this batch. Repository governance still has 17 blockers.
No protected Candidate, managed-provider acceptance, publication, rollout or
user update was attempted.

## 2026-07-15 - Administrator operations, backend-owned Skills and v0.3 session discovery are productized

The administrator requirement was previously split between release controls,
static deployment variables and UI-only model names. Changing a provider Key
or upstream model therefore required editing service configuration and a
restart, while user/quota operations had no single transaction and audit
authority. A new management schema and repository now own user revisions,
immutable usage adjustments, encrypted model revisions, idempotency results and
an integrity-chained audit trail. The administrator Web workspace is limited to
four sections: users, usage, models and releases. Its user filters, create/edit,
quota correction, model draft, bounded real test-and-activate, targeted rollout
and full rollout actions all call authenticated backend contracts.

Model credentials are AES-GCM encrypted with a deployment SecretProvider key;
the browser receives only a short fingerprint. Chat, image generation and
image edit have independent managed slots. A passed test activates one frozen
revision transactionally. Gateway requests acquire the current active chat
revision at request start, retain it for the complete stream and retire the old
client only after its active references drain. Image admission persists the
configuration ID, revision and upstream model into the Job, so retry/recovery
continues on the same tested revision across a later administrator change or
process restart. No new request requires a Python edit, Web rebuild or service
restart. The single-node shared-database and fixed-origin boundary, rotation,
rollback and failure procedure are now durable in
`admin-management-runbook.md` and linked from all three production runbooks.

The extensions surface had the same ownership problem: React inferred skill
category and whether an item was protected. The Runtime projection now owns a
v0.3-compatible taxonomy and safe icon key, while the service rechecks every
enable/disable action. Core-bundle skills and required core capabilities cannot
be disabled; optional signed skills remain controllable. The old extension
modal was replaced by a deferred Skills workspace with market/installed views,
backend categories, detail pages, local signed-ZIP installation and a collapsed
required-skills group. The Sidebar account footer now uses the authenticated
user name and a real lease-bound logout; model menus reuse provider icons.

The final v0.3 Sidebar discovery contract is also retained. General sessions
and each project's sessions independently show eight rows until “查看更多” is
opened. Current, pinned and running sessions are unioned into the collapsed
projection first and therefore can never disappear merely because their
history position is old. Expansion is view-only and cannot reorder or mutate
backend Threads. The dedicated 12-general/11-project browser scenario verifies
8 → all → 8 for both scopes and keeps a running old session visible.

During full browser verification two production-only defects were exposed and
fixed at their roots: named/default lazy exports could render a React #306
white screen, and several accessibility/session mocks hid genuine product
contracts. Lazy export ownership is now explicit; the browser guard always
reports console/page errors; clipboard permission, reload persistence, forced
colour focus, disabled-button semantics and compact Sidebar density are tested
as their real browser contracts. The deleted extension dialog had also left
roughly 590 lines of unreachable CSS; that entire selector block is removed
instead of carried as a compatibility skin. The release build remains below budget at
459.76 KiB raw / 146.20 KiB gzip initial JavaScript, with 24 chunks and
136.30 KiB of deferred features.

Exact focused evidence for this source is 94 Python tests passed, two explicit
environment skips and one upstream warning; all 180 Web contract tests pass.
The content-addressed Chromium suite passes 45/45, including administrator
users/models/full rollout, Skills, logout, task continuation, v0.3 “查看更多”,
reasoning persistence, image/retouch, share, accessibility and responsive
paths. Ruff, Python compilation, the 675-file source admission gate and the
483-production-file supply-chain/secret scan pass. The supply-chain inventory
is `927cf1735d3271a1325973504e17bd4edf81b5373e89624f7db989a15c125cd7`;
its ignored 21,857-byte report has SHA-256
`e1239bd1c5fc910f87d04a2ec84107b5303fb14687bc666798c5048dc9f18c95`.
No protected Candidate, publication, rollout or installed-user update was
attempted.

## 2026-07-16 - Hosted quality-gate drift is closed without weakening either contract

The first hosted run for the administrator/Skills/session-discovery checkpoint
correctly failed Ubuntu while Windows and both macOS architectures passed. The
failure was deterministic rather than a Runtime race: the new compact Turn
copy affordance referenced two CSS names that had never been declared, ten
focus states still carried literal one-pixel shadows, and the new explicit
administrator schema manager had not been added to the server migration
authority allowlist. The design and schema gates therefore caught integration
debt that focused feature tests could not see.

The correction reuses the existing desktop target and small-icon tokens,
introduces semantic subtle focus-ring and inset-ring tokens (including forced
colour mappings), and routes every affected focus state through them. The
administrator schema remains in its dedicated immutable migration manager; it
is now the ninth exact server schema authority, while every business
repository remains forbidden from issuing DDL. Neither gate was relaxed and no
runtime behavior or database shape was changed.

The exact corrected source passes the two gates directly and 13 affected
schema/management/image tests. A fresh Windows full Runtime run completes
1,942 passed / 17 explicit platform-environment skips / 0 failed in 1,538.16
seconds; the earlier 15-minute aggregate timeout was reproduced as an
insufficient outer budget, not a hung child. TypeScript, all 180 Web contract
tests and the content-addressed build pass; the build remains 24 chunks,
459.76 KiB raw / 146.20 KiB gzip initial JavaScript and 136.30 KiB deferred
features. Ruff/compile, the 675-file source gate, npm audit and `git diff
--check` pass. The supply-chain preflight covers 23 Runtime packages, 282 npm
packages and 483 production files; inventory SHA-256 is
`9320f611ff9881daa01cf4dc4902197361c4f4d058e742457a3cf7c6ff77ae19`
and the ignored 21,857-byte report SHA-256 is
`39696bfdd07b68128d6928cfc82b7c9e56c3a98eac8e11d2679b7c1e7cdeed1e`.
The corrected hosted matrix then passed on exact source
`f3142be9545c87ec461a9478f6c1771c14ea9266`: run `29435356727` completed
Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability
with all five Jobs successful. Draft PR #2 remained CLEAN and MERGEABLE. This
is still read-only source evidence; no protected Candidate, publication or
rollout is claimed.

A fresh post-correction Chromium run also passes 45/45 in 2.6 minutes. It
covers both themes and all locked viewports, zero axe violations, forced
colours/reduced motion, compact copy feedback, v0.3 “查看更多”, Skills,
administrator operations, reasoning persistence, image preview and structured
Retouch. This replaces the earlier browser evidence for the corrected source.

## 2026-07-16 - Main governance closure and Environment compensation

PR #2 was squash-merged into `main` at exact source
`c8fd385c5600664a2f9217c64773af5fed2fd21f`. Main run `29436909984`
passed Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte
stability. Selected GitHub-owned Actions, read-only workflow permissions, the
five strict required checks, administrator enforcement, required review,
linear history and conversation resolution are now active. All four reviewed
v1 workflows are enabled on main; the live audit consequently falls from 17
to 10 blockers.

Applying the protected Environment contract exposed a GitHub API transaction
boundary: on this private user-owned repository plan, the Environment PUT
creates an empty Environment and then returns HTTP 422 because required
reviewers are unsupported. The initially orphaned `ecorex-release-stage` was
verified and removed. No reviewer-free substitute was created, no repository
visibility was changed and no Candidate or release job was dispatched.

The governance client now checks whether each Environment existed before its
write, boundedly classifies the GitHub rejection, and deletes only an
Environment that was absent before the failed attempt. A pre-existing
Environment is never deleted. The JSON receipt carries a non-sensitive stable
error and explicit compensation fact; a cleanup failure remains visible as a
separate retryable error. Thirteen focused governance tests and Ruff/Python
compilation pass. A real bootstrap invocation against exact main reproduced
`github_environment_reviewers_plan_unsupported`, returned `compensated=true`
and left the repository with zero Environments, proving the compensation path
against GitHub rather than only a mock transport. Fifty-seven adjacent
Candidate, handoff, publisher, gate and evidence tests also pass with one
explicit environment skip; the 675-file source, dependency-lock and both
Runtime/server schema-authority gates remain green.

Draft PR #3 run `29438953446` on exact source
`f49f187d3a114aeb4312f62dfb0a5867221257bd` then passed Ubuntu quality,
Windows x64, macOS arm64/x64 and Cross-runner byte stability; all five Jobs
succeeded. This is source and governance-tool evidence only and does not
substitute for a protected Candidate or live release receipt.

The remaining ten blockers are the six named protected Environments and four
distinct online role-labelled Runners. The v0.3 “查看更多” behavior remains an
independent browser-tested v1 contract: eight collapsed rows per general or
project scope, with current, pinned and running sessions always included.

## 2026-07-16 - Current-main Windows full-Pack Candidate drill

PR #3 was squash-merged as exact main
`701aa4228635acb9584703592110193412dce600`. Main run `29439964797`
passed all five required Jobs. Its Ubuntu quality Job completed 1,931 Python
tests with 32 explicit platform/environment skips, all 180 Web contracts, the
24-chunk production build and every static gate. A fresh read-only repository
audit remained fail-closed at exactly ten findings and action `none`; its
2,669-byte report SHA-256 is
`c411da41c9ac289a4f84acaeb0345d2494cbf54e933172759db65846ff7c6d48`.

The current-main local Candidate prerequisite was then rebuilt in a detached,
clean temporary worktree so the user's untracked `.artifacts/` could neither
be modified nor make source provenance dirty. `npm ci`, generated-contract
freshness, TypeScript and the production Web build passed before staging. The
source-pinned Windows stager used Go 1.26.5, MSVC tools 14.44.35207/compiler
19.44.35227.0 and Windows SDK 10.0.26100.0, produced Core, Bootstrap and all
six required Pack receipts, and kept `worktree_dirty=false`.

The zero-publication signed-candidate drill passed in 3,051.188 seconds. First
install and healthy replacement both completed with HTTP 200; the v0.3
released-schema fixture migrated copy-on-write to a committed receipt and
restarted after source removal. Three durable drain checkpoints preceded
activation. The signed source order was domestic mirror, GitHub and CDN; an
injected mirror outage resumed from zero on GitHub. Bad digest bytes were
rejected without changing the active slot. A faulted replacement rolled back,
returned HTTP 200 on the prior slot and discarded the fault slot. Private keys
were not persisted, no external endpoint was contacted and the 3+ GiB
disposable install/candidate root was removed.

The redacted 43,536-byte full report has SHA-256
`3fd04faf117a0a2c535bf1ec5aa12a0e615364c6b50df44c695ef179ac02cf3c`;
its tracked review summary is
`evidence/windows-signed-candidate-main-2026-07-16-summary.json` (3,716 bytes,
SHA-256 `3635925c8569b59a17922969251546cbb1f938ef1a79243e4dea1b1c5f800453`). The report
explicitly remains `local-windows-drill`: only 8 of the fixed 24 platform
receipts were locally produced, 16 protected macOS receipts are absent, the
real user v0.3 corpus was not claimed and no Candidate promotion, publication
or rollout occurred.

Seventy-one Candidate/ReleaseBuilder/Updater/activation contract tests pass
with one explicit environment skip. Ruff, Python compilation, both JSON files,
diff and the 675-file source gate pass. An initial parallel test/compile launch
made pytest imports and `compileall` replace the same Windows `.pyc` and
produced WinError 5; the orphaned test was allowed to finish, then tests and
compile were rerun serially. The serial evidence replaces that tooling race;
no production source or gate was relaxed.

## 2026-07-16 - User-deferred protected infrastructure and local live-preflight continuation

The user explicitly deferred provisioning the six protected GitHub
Environments and four isolated role-labelled Runners so useful local work could
continue. This does not waive, satisfy or relabel the protected provenance
contract: a local result cannot create an official Candidate-bound
`live-model`, `live-image` or `cdp-acceptance` receipt, and no publication or
rollout was authorized through the deferral.

The reviewed evidence batch is now merged to exact `main`
`84aeed15a81463ff9bfcdd7dceeda992ee692708`; hosted run `29445710112` passed
Ubuntu quality, Windows x64, macOS arm64/x64 and Cross-runner byte stability.
A fresh local WebUI preflight on that exact source passed all 45 Chromium tests
in 167.3 seconds. It covers both themes and all locked viewports, Codex density,
model selection and vendor icons, quota/context display, completion timing and
copy feedback, task pinning/continuation, independent general/project v0.3
“查看更多”, Skills, full-access revocation, sticky reasoning replacement,
retry/reconnect, persisted HITL, connector login, queue reachability, share
copy, fit-first image preview, structured precise-retouch, forced colours,
reduced motion and touch actions.

The matching local Runtime preflight passed 226 focused tests in 172.4 seconds.
It exercises GPT-5.6 SOL medium/272,000 policy projection, managed model
catalogue and dynamic image revisions, ranked non-exclusive image intent,
progressive `tool_search`/`tool_describe` disclosure without hiding read,
fetch, vision, CDP, shell or imagegen, the 128-concurrent idempotent image
admission case, lease fencing, provider uncertainty/restart recovery, shared
generate/retouch scheduling, Retouch concurrency linearization, crash recovery,
stable retry identity and late-result rejection. These are deterministic local
product checks; real managed-provider connectivity remains separate
live-preflight work.

The new repository-owned local CDP driver was then committed as
`622921fbcc2be16d73209bfad2b7ff0cea19afc7`. It launches the machine-installed
Google Chrome with a fresh temporary profile and explicit loopback remote
debugging port, connects with `connectOverCDP`, blocks public network access,
runs the fixed 18-scenario matrix and four locked viewports, hashes every
screenshot in memory, and cleans the Browser, GA Runtime and profile in a
bounded `finally` path. An initial driver run exposed three harness defects:
the total timeout lost its scenario identity, animation sampling occurred
before the 160 ms hover transition settled, and navigation-cancelled SSE was
misclassified as a failed request. Per-scenario deadlines, stable progress
identifiers, animation convergence and the exact `/events/stream` navigation
exception corrected those root causes without hiding any other request error.

The exact committed driver passed against Chrome `150.0.7871.115` in 24.261
seconds: 18/18 scenarios, four viewports and 131 assertions, with zero console,
page, local-request or external-request failures. Chrome process count and
owned temporary-profile count were both zero after exit. The 2,981-byte
redacted evidence is tracked at
`evidence/local-live-preflight-622921fb-2026-07-16.json`; its SHA-256 is
`7d67be5f37216bcae18f98715747c2dc24dcd18fcf358f97fa8be8edd38020d1`.
It explicitly declares `candidate_bound=false`,
`protected_provenance_claimed=false` and
`runtime_source=local-ga-contract-runtime`: native Chrome/UI behavior is now
locally proven, while an installed signed Runtime plus real managed Model/Image
transports are not.

## 2026-07-16 - Real v0.2.9.2 preservation and deletion-authority drill

The migration authority now accepts an explicit legacy source version and
supports both `0.2.9.2` and `0.3.0` without weakening the release-marker and
schema checks. The compatibility `migrate_v030_to_v1` entry remains available,
while the product, CLI, Bootstrap, inventory, completion receipt and target
authority all carry the selected source version. Source version is included in
the migration identity, preventing a receipt for one legacy version from being
replayed against another.

The canonical legacy `sessions` database is the deletion authority. UI cache
may enrich the title, summary and pin state only for a session that still exists
in that database. A cache-only session is treated as previously deleted and is
never recreated as a v1 Thread. The real installed v0.2.9.2 corpus contained 93
such stale cache IDs; all 93 were excluded and zero deleted sessions were
restored. The same plan preserves 54 live sessions, 1,029 messages, 54 session
summaries, two projects and three live project bindings.

The real corpus also exposed 42 request IDs reused across 169 Turn occurrences.
The migrator now retains every conversation Turn and marks the ambiguity in its
metadata, but never binds one legacy run ledger row to multiple Turns. This
fixes the historical identifier defect without dropping conversation content or
inventing execution relationships.

Exact commit `0916bd04465a23504e989bbccf7960273827eadf` completed a read-only,
copy-on-write dry-run over 897 source entries / 459,541,787 bytes. It planned 54
Threads, 1,029 messages, 580 Turns, 580 input revisions, 247 legacy runs and
38,073 run events; five secret-bearing entries were quarantined. The inventory
SHA-256 is `7bd10f200ff9917204b6edd2b7a33f908674ec95e0960da12870b3572b8156cf`.
Source inventory was unchanged before and after, no target was published and no
content, path or secret was persisted. The redacted evidence is
`evidence/v0292-real-user-data-dry-run-0916bd04-2026-07-16.json` (1,829 bytes,
SHA-256 `ca606a5c7fd820279ebfc259b31a814e196ce99750e57b2d58c5f87637c066b8`).

Focused v0.2.9.2 tests pass 2/2. The complete migration, activation, quarantine
and storage set passes 68 tests with two explicit environment skips and one
unchanged Starlette warning. Ruff, Python compilation, JSON validation, diff
check and the 676-file source admission gate pass. This proves preservation and
non-resurrection at planning/import authority; installed signed-v1 activation
and health checking remain a separate release gate.

## 2026-07-16 - Real v0.2.9.2 commit-mode import and idempotent replay

After PR #5 passed all five hosted jobs and merged as exact main
`e1d874c51e6bd6f7d05844ed4c12ad40b9b57962`, the same installed v0.2.9.2
corpus was imported into an isolated disposable v1 target with an ephemeral
quarantine key. This was a real commit-mode copy-on-write import rather than a
dry-run. It did not replace or stop the installed legacy Runtime.

Exact-main hosted run `29461021847` subsequently passed Ubuntu quality,
Windows x64, macOS arm64/x64 and Cross-runner byte stability. All five Jobs
completed successfully on the merge commit.

The published temporary target contained exactly 54 Threads, 54 legacy-session
mappings, 1,029 message Items, 54 summaries, 580 Turns, two Projects, three live
Project bindings, 247 legacy runs and 38,073 run events. SQLite
`integrity_check` returned `ok`. The migration report recorded 93 cache-only
session IDs excluded and zero previously deleted sessions restored. A second
execution against the same target returned the completed report as an
idempotent replay and retained a single migration-run row.

The first verification attempt had already completed the import but used the
nonexistent verifier column `items.item_type`; the resulting read-only query
failed and the temporary target was automatically removed. The verifier was
corrected to the schema-authoritative `items.kind` column and the complete
import/replay was rerun from a clean target. No product code or gate was changed
to hide the verifier error.

Both successful import runs used source inventory SHA-256
`7bd10f200ff9917204b6edd2b7a33f908674ec95e0960da12870b3572b8156cf`.
The final target and ephemeral key were removed after verification; source data
was not mutated. The aggregate-only evidence is
`evidence/v0292-real-user-data-import-e1d874c5-2026-07-16.json` (1,622 bytes,
SHA-256 `60a7e8d8de54bfd51b0d84dd37e4d0cef1bb45d2044a5ee80ef6521144ce892f`). Signed-v1
side-by-side activation and post-health count verification remain separate
release gates.

## 2026-07-16 - Exact-main signed Runtime repeatability failure and bind hardening

Exact main `3dee8fdc882984aaa00b2571859556f178f88aab` passed hosted run
`29461827830` on Ubuntu, Windows x64, macOS arm64/x64 and Cross-runner byte
stability. A new detached clean worktree then installed the locked Web
dependencies with zero vulnerabilities, passed generated-contract/type checks
and built the 25-asset/24-chunk production Web bundle at 459.76 KiB raw / 146.20
KiB gzip initial JavaScript.

The first complete local Windows signed-Candidate ceremony failed cleanly after
1,773.9 seconds: Bootstrap reported one launch, exit 70 and the old generic
`software` startup stage. It wrote no success report, published nothing and did
not mutate the installed user Runtime. The same exact source and freshly signed
artifacts were rebuilt under debug-safe cleanup policy. The second ceremony
passed in 2,471.86 seconds: first install, migration source-removal restart,
update-and-refresh and rollback all returned HTTP 200; three durable drain
checkpoints preceded activations, a corrupt digest was rejected, the fault slot
was discarded, private keys were not persisted and the disposable Candidate was
removed. The 43,535-byte redacted full report SHA-256 is
`1987dd3887be202644edcf16a07a96ee325b8794ac55eebd1b16bcac2d85a31e`.

The old diagnostic did not preserve the ceremony phase, so the root cause is a
supported inference rather than a direct proof. After the ASGI application and
Uvicorn Config have composed, Uvicorn `SystemExit` is the listener boundary.
The drill previously selected `bind(0)`, closed that ephemeral port, then spent
time verifying the signed slot and Packs before child creation. This left a
real port-reuse TOCTOU window and matches an isolated generic `software` exit
followed by an identical-source success.

The hardening keeps a scanned 20000-29999 loopback port bound through signed
slot and Pack verification, releases it immediately before process spawn,
records the fixed ceremony phase in future Bootstrap failures, and classifies
post-composition Uvicorn `SystemExit` as `http_server_bind` rather than an
unknown software crash. This narrows the pre-spawn reuse window but is not
described as a proof that all listener races are impossible. Focused Runtime
entrypoint/Candidate tests pass 50 with one platform skip; the combined
Bootstrap/update/Runtime/Candidate regression passes 94 with two skips. Ruff,
Python compile and diff checks pass. The aggregate evidence is
`evidence/windows-signed-candidate-main-3dee8fdc-2026-07-16-summary.json`
(4,245 bytes, SHA-256
`66ef2a5d5d3c8d1f63b29556dc9cf13d57d9079781360955b278d83c8dfc6941`).

This remains a local Windows drill with 8/24 platform receipts. It does not
replace protected macOS receipts, a signed installed-live CDP run, managed
Gateway/device-session acceptance, release publication or rollout.

## 2026-07-16 - Administrator Image 2 direct-provider boundary

The administrator model database previously activated and froze image model
revisions correctly, but the default dynamic adapter still treated an
`openai_compatible_image` preset as another EcoreX `/v1/image/jobs` service.
Changing the Image 2 key/model in the administrator UI therefore was not a
complete production path, and structured Retouch digests were never converted
to real upstream image/mask bytes.

The cloud Image Orchestrator now owns a direct bounded adapter. Generation uses
the fixed allowlisted `/v1/images/generations` route; Retouch reads the frozen
base, references and PNG mask from shared CAS and uses multipart
`/v1/images/edits`. It accepts only inline Base64 image data, validates size,
signature and SHA-256, never follows an upstream URL, never exposes the key to
the local Runtime and keeps each Job on its admitted configuration revision.
The internal grayscale selection mask is resized with nearest-neighbour
sampling to the first image, converted to RGBA and has its selection semantics
inverted to the upstream alpha contract (selected pixels become transparent).
When a mask is present, a JPEG/WebP/AVIF base is transcoded to a matching PNG
inside the configured pixel and byte envelope. Pillow 12.3.0 is locked only in
the development/cloud profiles; it is not added to the local Runtime Core.
The adapter also enforces the current GPT Image 2 flexible-size contract before
any billable request: 16-pixel edge alignment, a 3:1 maximum aspect ratio,
3840-pixel maximum edge and the documented 655,360–8,294,400 total-pixel
window. Decoded RGBA pixels must fit the configured image memory envelope, so a
known-invalid or locally unretainable response cannot be requested first and
rejected only after the provider may have billed it.

The cross-layer audit also removed a large-image Retouch contradiction. The
Artifact domain intentionally compiles deterministic ROI masks to at most
2048px/4,194,304 pixels, but the cloud adapter previously required that bounded
mask to equal the original edit-surface dimensions. A 3840×2160 image therefore
failed before reaching the provider that was designed to restore the mask. The
adapter now re-compiles the mask from the immutable full-size edit surface and
typed annotations, verifies its bytes, digest, dimensions, coverage and regions,
then permits the direct provider's nearest-neighbour restoration. The cloud Job
also freezes the edit-surface width and height instead of silently using the
old 1024×1024 defaults, and the provider verifies the returned dimensions.
Malformed or substituted masks and unsupported source dimensions still fail
closed.

Masked JPEG bases now apply EXIF orientation before lossless PNG normalization,
so the edit surface, ROI mask and provider pixels share the same user-visible
coordinate system. A dedicated 32-submit asynchronous test proves the direct
adapter never exceeds its hard four-call semaphore even when all submissions
arrive together; this is adapter-level boundedness, not a provider soak claim.

The failure policy is deliberately billing-safe. A submit timeout, transport
failure, 408/425 or 5xx becomes `provider_uncertain`; `recover` never resubmits
because the synchronous Images API has no authoritative lookup route. An
explicit 429 remains retryable with a bounded delay. Total Retouch inputs are
bounded to one `max_image_bytes` envelope. Admin direct mode now requires
`worker_concurrency * max_image_bytes * 6` memory at startup, accounting for
multipart inputs plus Base64 JSON decode peaks; insufficient deployments fail
closed before accepting work.

The focused current-source set passes 122 tests with two explicit skips and one
unchanged Starlette warning. It covers exact generation payloads, model health,
CAS-backed base/reference/mask multipart, JPEG base conversion, exact mask
alpha/size semantics, EXIF-oriented coordinates, bounded 4K ROI restoration,
32-submit direct-adapter concurrency, flexible-size
boundary/admission, decoded-memory
preflight, output dimensions, adversarial JSON depth, secret-safe
transport failures, no URL fetch, no recovery resubmit, bounded delta/date 429,
concurrency serialization, aggregate input admission, dynamic revision
selection, production resource validation, storage and managed image
integration. The adjacent v0.2.9.2 deletion-authority/product migration
regression also passes 22 tests: cache-only deleted sessions remain excluded.
The first complete-suite attempt started twice because a background launcher
appeared idle before its delayed child became visible. The duplicate processes
contended for CPU and SQLite; that run exposed one real stale dev-dependency
expectation after Pillow was added and one 50 ms maintenance-thread scheduling
miss. The dependency contract was corrected, the timing case passed eight
isolated repetitions, and all duplicate processes were removed. A clean
single-process rerun then passed 1,970 tests with 17 explicit environment or
platform skips and zero failures in 1,748.33 seconds. Ruff and diff checks pass.
The locked Web workspace reports zero vulnerable packages; generated Runtime
contracts and TypeScript pass, all 180 Web contract tests pass, and the
production build emits 25 content-addressed assets / 24 JavaScript chunks.
Initial JavaScript is 459.76 KiB raw / 146.20 KiB gzip under the unchanged
475/150 KiB limits. The design debt, strict legacy cutoff, public download,
dependency lock, Runtime/Server schema authority, reproducibility and 678-file
source admission gates also pass.

Draft PR #8 pins implementation commit
`9b893ce9079b2cb1b90b951a448b27bbea2620f2`. Hosted Actions run
`29474142345` passed all five Jobs: Ubuntu quality and deterministic build,
Windows x64, macOS arm64, macOS x64 and cross-runner canonical-byte stability.
This is exact hosted source evidence for the implementation commit; it is not
a protected signed Candidate, managed-provider acceptance or release approval.
This is deterministic local contract evidence, not a real managed Image 2
connectivity, precision score, protected Candidate, publication or rollout
claim.

## 2026-07-16 - Exact-main signed Candidate and live-boundary audit

PR #8 merged to exact main
`90539b2fce55f2bbd20c552d68b07135b75e7742`. The independent main push run
`29474876004` passed Ubuntu quality/deterministic build, Windows x64, macOS
arm64/x64 and cross-runner byte stability; all five Jobs completed.

The exact-main local Windows signed-Candidate ceremony passed in 2,203.25
seconds. First install, committed migration restart, same-version update and
refresh, and the restored rollback Runtime all returned HTTP 200. Three durable
drain checkpoints preceded activation attempts; a corrupt digest was rejected,
the fault slot was discarded, the private key was not persisted and the
temporary Candidate was removed. Core is 21,535,788 bytes and Bootstrap is
3,109,078 bytes. The 43,534-byte redacted full report has SHA-256
`9495383f...d5b3d2`. The tracked aggregate receipt is
`evidence/windows-signed-candidate-main-90539b2f-2026-07-16-summary.json`.

The live audit deliberately did not relax security to finish the checklist.
The existing local administrator credential still reaches the legacy HTTP
provider: a real catalog request contains `gpt-5.6-sol`, and a real Responses
request completed with medium reasoning and the 272,000 compaction policy.
That is a direct diagnostic only. The v1 image adapter requires a fixed HTTPS
origin; the legacy origin is HTTP and the same host does not accept HTTPS, so
Image 2 live execution was blocked before the provider rather than downgraded.
No v1 managed device-session credential exists on this workstation.

The ChatGPT Chrome Extension is installed and enabled, but Chrome was not
running and the native messaging registration is absent. Candidate-bound CDP
was therefore not executed; the registry was not self-repaired or bypassed.
The existing real v0.2.9.2 import evidence still retains 54 authoritative
sessions, excludes 93 cache-only deleted IDs and restores zero deleted
sessions. This Candidate ceremony used the released v0.3 schema fixture, so a
real v0.2.9.2 signed activation is not claimed.

Promotion remains closed: the local drill has 8/24 platform receipts, while 16
protected macOS receipts, a managed HTTPS Gateway/device session and restored
Chrome control are absent. No release publication, rollout or user update was
attempted.

## 2026-07-16 - Parallel release-blocker integration before the next Candidate

Three independent blocker lines were integrated and then reviewed again on the
main task. The administrator model test no longer treats catalog visibility as
activation proof. An explicit test now performs Catalog plus exactly one real
operation on the frozen revision: Responses, Chat Completions, Images
Generations or multipart Images Edits. Submitted POST timeouts, transport
losses, 408/425 and 5xx are `provider_test_uncertain`; the Control Plane does
not retry or replace the active revision. Readiness never invokes this path.
The operation has a separate 30–600 second production timeout (180 seconds by
default), bounded response bodies/concurrency, HTTPS-only fixed origins and
in-memory-only response validation.

The Windows signed-Candidate drill now defaults to the exact v0.2.9.2 release
tag schema and includes a cache-only deleted-session fixture. An optional
operator-selected legacy root is inventoried, copied to a disposable stable
snapshot and inventoried again; migration and deletion checks run only against
that snapshot. The deletion gate reuses the product migrator's exact database
candidate order and released conversation adapter instead of maintaining a
second SQLite approximation. Target aggregate counts, read-only integrity and
the intersection between cache-only IDs and imported legacy session mappings
must all pass before the activation evidence is accepted. The source is never
deleted or used as the v1 target.

A Candidate callback boundary can run a fixed real-Google-Chrome CDP harness
only after the signed fault Candidate has reached an authoritative rollback
terminal and the restored slot is current, known-good, receipt-valid and
sandbox-attested. The callback is rechecked after execution, uses a bounded
Windows Job, an isolated profile and no ambient provider/proxy credentials.
Chrome chooses its own ephemeral debug port and publishes it through
`DevToolsActivePort`; response bodies and evidence are bounded. The current
harness deliberately labels its result `unauthenticated-shell-smoke` and
declares that full office scenarios and promotion were not proven. It cannot
substitute for the requested authenticated image/tool/steer/Retouch matrix.

Root review added server-error no-retry coverage, migration candidate-order
coverage, read-only target verification, streaming response limits and the
explicit smoke evidence scope. The complete Python suite passes 1,996 tests
with 17 explicit skips, zero failures and zero errors (2,013 JUnit cases) in
1,095.247 seconds. The 106-test changed-boundary set, compile/lint and all
static product gates pass. Web generated contracts, TypeScript and 180/180
tests pass; the production bundle remains 459.76 KiB raw / 146.20 KiB gzip
initial JavaScript across 24 chunks, and the dependency audit reports zero
vulnerabilities.

These are implementation and local deterministic gates only. The next exact
source signed-Candidate ceremony, real managed HTTPS provider/device session,
authenticated browser matrix, protected macOS receipts and publication remain
separate gates. No rollout or user update was attempted.

## 2026-07-16 - Exact-source v0.2.9.2 signed activation ceremony

Implementation commit `d60d9cda8c2ef9d183b2f5b0e331e9cf8de36b7b`
was checked out into a clean detached worktree. The locked Web install reported
zero vulnerabilities and rebuilt the same 25 immutable assets / 24 chunks at
459.76 KiB raw / 146.20 KiB gzip initial JavaScript. The worktree remained
clean before the source-pinned Windows stage began.

The signed Candidate ceremony passed in 2,170.156 seconds. Eight local Windows
Core/Bootstrap/Pack receipts were generated. First install waited for explicit
confirmation and its signed Runtime returned HTTP 200. The default migration
gate used the exact v0.2.9.2 tag schema commit
`b52999b07a753e103a993a4da9d3c83c3f366e71`: two authoritative fixture
Threads, two messages, two summaries, one Project and one binding were imported
copy-on-write; one cache-only deleted session was excluded, zero deleted
sessions were restored and SQLite integrity was `ok`. The disposable snapshot
was removed before a second signed Runtime restart returned HTTP 200.

The same ceremony then completed a confirmed background update and refresh,
rejected a corrupt digest without pointer mutation, activated a signed fault
Candidate and restored the previous known-good slot with rollback HTTP 200.
Three durable drain checkpoints preceded activation attempts. The fault slot
was removed, private signing material was not persisted, and the complete
temporary Candidate directory was removed.

The untracked 44,008-byte full report has SHA-256
`22c1078b...e5d37c`. The aggregate-only tracked summary is
`evidence/windows-signed-candidate-d60d9cda-2026-07-16-summary.json`.
This ceremony proves the exact released v0.2.9.2 schema/deletion contract inside
the signed activation chain, but its corpus is a deterministic release fixture.
It does not relabel the earlier real installed import as signed activation. The
real import evidence remains 54 retained authoritative sessions, 93 excluded
cache-only IDs and zero restored deleted sessions.

Installed-signed CDP was intentionally absent from this ordinary ceremony: the
new lower-level harness is explicitly an unauthenticated shell smoke, while the
requested authenticated image/tool/steer/Retouch matrix still requires restored
browser plugin control and a managed test session. Protected macOS receipts,
managed HTTPS Model/Image acceptance, publication and rollout remain closed.

The implementation/evidence head
`f7c14d1499a296ea52ef3822a4bd9846b92e8827` then passed hosted pull-request
run `29485934540`: Ubuntu quality/deterministic build, Windows x64, macOS
arm64, macOS x64 and cross-runner canonical-byte stability all completed
successfully. Hosted compatibility and byte evidence do not supply protected
native signing receipts or live provider/browser acceptance.

## 2026-07-16 - Direct-production publication unblock

The operator explicitly waived the separate manual/CDP/HSM approval step and
authorized direct production publication. The waiver is recorded as `WAIVED`,
never as a passing protected gate. Old production traffic remains unchanged
until the exact new Candidate is healthy and all public bytes are read back.

The first platform-stage run `29506205694` correctly failed before it could
emit a complete three-platform input set. Root causes were isolated and fixed:
the Windows self-hosted runner now installs digest-pinned Python 3.11.9 through
digest-pinned uv without registry mutation; `onnxruntime` is pinned to 1.23.2,
whose CPython 3.11 macOS arm64/x64 wheels are present in the hash lock; and the
GA tests read security and cookie headers through Node's native HTTP response
instead of the platform-variable Fetch projection.

Target-host smoke testing found three additional deployment defects before
traffic mutation. The wheel omitted verified administrator static resources,
so package-data is now explicit and a real wheel/zipimport test loads them.
Alibaba Cloud Linux exposes PostgreSQL through `postgresql.service` and
`/usr/bin/psql`, so the deployer and dependent units now bind those authorities.
The existing TLS server also owned four legacy Admin locations. The deployer
now moves those exact locations behind a fail-closed legacy include, keeps them
active while the Candidate starts, switches both service and Admin routes only
after health, and restores both symlinks if Nginx validation or reload fails.
Real production configuration passed offline legacy and Candidate `nginx -t`
assembly; no live Nginx route was changed during this work.

The release contract now binds public
`zhangyifanjackson-dotcom/EcoreX-installers`. `ghproxy.net` is a read-only
GitHub view: GitHub draft assets and CDN are made ready, GitHub becomes public,
then every mirror byte is downloaded without redirect or encoding and checked
against the signed SHA-256. A real old-release probe returned HTTP 200, zero
redirects, 105 exact bytes and the expected digest. No mirror write credential
exists.

Root review also fixed a Windows descriptor/path stat mismatch. Windows can
project different `st_ctime_ns` meanings through `lstat` and `fstat`; stable
handle identity now uses volume, file ID, size and last-write time while both
path checks retain creation/change time, mode and reparse attributes. Three
previously deterministic Candidate failures now pass, as do 100 consecutive
Windows executable fixture reads, without weakening path-swap detection.

Focused Python evidence is 27 cloud/deployment/package tests plus 61 release
tests, all passing. Ruff, dependency-lock and diff gates pass. Web generated
contracts, TypeScript, 180/180 tests and the content-addressed production build
pass with 24 chunks and 459.76 KiB raw / 146.20 KiB gzip initial JavaScript.
Publication remains pending: corrected source must merge to main, the ephemeral
Windows runner must be re-registered, all three real stages regenerated in one
run, and exact-main cloud/client artifacts signed before deployment.

## 2026-07-16 - Direct-production transaction and public-cutover closure

The direct-publication exception is now a separate, fail-closed contract rather
than a mutation of the normal gate table.  A release-key-signed prepare/finalize
admission binds the exact stable manifest, Candidate receipt, operator waiver,
publication-key-signed three-source receipt and Bootstrap readback proof.  Only
`live-model`, `live-image` and `cdp-acceptance` may be recorded as `waived`; they
are never projected as `passed`.  Every other required gate must pass, prepare
and finalize are append-only/idempotent, the production exception is disabled
by default and can name only one release ID plus one operator-instruction
digest.  The exceptional request body is capped at 32 MiB before JSON parsing,
with a matching route-scoped Nginx limit that does not widen ordinary Admin
requests.

The cloud activation journal now records `migrating` before stopping either
writer set and records `schema_ready` only after the idempotent migration and
all schema gates pass.  Source and target writers are never active together.
Recovery classifies both live Nginx routes, verifies all four service roles and
rolls forward after a possible target write.  A deterministic first-release
migration failure restores the immutable legacy source; a v1 source is restored
only after schema compatibility succeeds.  The journal unlink remains the
sole commit point.  A read-only production corpus dry-run retained 40 active
users, excluded seven deleted users, retained eight eligible sessions, excluded
248 revoked and 114 expired sessions, and reduced 2,088 usage rows into 2,061
authoritative aggregates while excluding 27 rows.  Unsafe historical public
HTTP OpenAI/Gemini/Image credentials are imported disabled with
`rotation_required`; the legacy database remains unchanged.

Release bytes can now be uploaded to the Control Plane's fixed CDN replica
namespace using a rotating current/next bearer token, exact length/digest and
kind validation, no-clobber content-addressed writes, fsync and crash recovery.
The client mirror retries only retryable transport/status failures with bounded
shared backoff; redirect, encoding, size and digest failures remain terminal.
The provider bridge uses a root-owned specification, loopback-only TLS,
private-CA hostname constraints, certificate/key/SAN/EKU checks, tagged hosts
ownership and a real TLS/OPTIONS probe.  Public or hostname HTTP upstreams are
rejected; HTTP is available only behind an explicit waiver for loopback/private
IP literals.

The Linux aarch64 cloud artifact builder is exact-source and wheel-only.  It
binds Python 3.11.9, lock digests, source commit, file modes and every file
digest, then exports a detached domain-separated payload for the Windows DPAPI
release key.  Linux attaches and verifies that signature only after rescanning
the staged tree.  The public Web/Admin deployer similarly requires a
release-key-signed site authorization binding the manifest, waiver,
publication/index/direct receipts and exact site-tree digest.  It uses the
shared product lock, content-addressed slots, a durable journal, atomic legacy
directory exchange/current pointer, Nginx validation/reload and real-SNI HTTPS
body/cache/Admin readback before it commits.  The Admin link is the exact
`/ecorex-agent/admin/` target.

Independent review then closed three pre-publication bypasses.  The 32 MiB
direct-admission allowance is an anchored PUT-only Nginx location, not a
release-admin namespace override.  An internal no-body `auth_request` verifies
the Bearer and `release_admin` role in Nginx's access phase before the proxy
buffers the client body; ASGI repeats authentication and admits only one
in-flight evidence body, returning 401/403/429 without reading rejected bodies.
The public download root is taken over under the shared lock as root-owned
0755 with the static reader group, while staging/legacy state remains root-only;
owner, mode, device, symlink and hardlink boundaries are rechecked before every
switch.  Admin readback is no longer any non-empty HTTP 200: the site
authorization derives exact index/CSS/JS/health identities from the same
signed cloud manifest, and online validation requires their byte digests,
cache policy, CSP, product-version header and canonical ready response.

Windows online-publication verification no longer compares CPython's
path-projected creation time with a handle-projected NTFS ChangeTime.  Stable
identity uses birth time while descriptor-before/after and final path reopen
retain ChangeTime, size, last-write, volume and file ID checks.  The new test
also detects same-size mutation with restored mtime, so the correction removes
the false failure without relaxing TOCTOU protection.

The final combined changed-boundary suite passes 206 tests with ten explicit
platform-conditioned skips and zero failures.  The cloud activation suite
passes 51 tests with four Windows skips; the expanded public-site/security
suite passes 40 tests with five Windows symlink skips.  Lint/compile,
dependency locks, Runtime/server schema authority (including the explicit
direct-admission migration authority), strict legacy cutoff and the public
download gate pass.  Publication is still pending the source commit,
PR matrix, exact-main platform/cloud builds, signatures, server migration,
three-source readback, Bootstrap activation and final live URL validation.  Old
production traffic has not been switched by this implementation batch.

## 2026-07-17 - Linux semantic correction before merge

PR run `29521151721` correctly withheld promotion.  Windows x64 and both macOS
compatibility jobs passed, but the Ubuntu full suite reported 14 failures from
three test-boundary defects that Windows had conditionally skipped: signed
cloud fixtures did not apply their declared POSIX modes; portable Provider
Bridge tests invoked production `fchown(root:root)` as an unprivileged CI user;
and portable public-site tests invoked production `lchown(root:994)`.  Three
Admin-route tests also assumed that the Control Plane Nginx file could never
gain unrelated routes.  No production traffic, manifest or user update was
created from this failed run; cross-runner byte stability remained skipped.

The fixes preserve every production fence.  Signed artifact fixtures now
materialize their declared 0755 executable and 0644 data modes.  Provider
Bridge atomic replacement still unconditionally sets root ownership, with
tests explicitly simulating and asserting that call; its durable order is
`fchown → fchmod → write → fsync → replace → parent fsync`.  Public-site
portable tests now simulate `lchown` alongside their existing rename/flock OS
boundary, while the deployed code still requires root:994.  Admin-route
validation now parses exactly seven required locations and verifies each
path/rewrite/header/upstream directive, rejecting missing, duplicate, mutated
or unexpected locations without forbidding unrelated Control Plane routes.

The main task reran the three affected domains on both platforms: Windows
passes 81 tests with 12 Linux-conditioned skips, and WSL Ubuntu with exact
Python 3.11.9 passes all 93 tests with zero skips/failures.  Ruff, compilation
and whitespace checks pass.  A new PR head and full hosted matrix are still
required before merge.

## 2026-07-17 - Connector late-success ownership correction

PR run `29522376431` proved that all 14 earlier Linux failures were removed:
Windows x64 and macOS arm64/x64 passed, while Ubuntu completed 2,178 tests and
failed only `test_late_inline_success_always_creates_recovery_delivery_item`.
The failure was not hidden with a workflow rerun.  The old test used a fixed
300 ms sleep, but the load-sensitive failure exposed a real state-machine gap:
an idempotent retry that observed `outcome_unknown` immediately returned manual
reconciliation even when the original provider completion lease was still
active and exclusively owned by its late-result watcher.

The repository now normalizes expired operation leases before classifying an
idempotency reservation.  `outcome_unknown` with a non-expired active provider
fence is `in_progress`, so a Runtime caller waits for the original owner; an
expired or inactive fence remains fail-closed `uncertain`.  Completion polling
uses the same contract.  Recovery delivery is derived from the durable result
stage's `completion_path=late_provider_result`, so a retry that wins local
finalization cannot suppress the one recovery Tool Item/event.

The regression no longer guesses scheduling with sleep or `call_later`.  A
barrier proves the retry entered `_await_invocation_completion` before the
gated provider result is released.  Tests also cover watcher failure after
staging, expired/inactive fences, three later replays, one provider call, one
recovery item/event and zero final leases.  Connector coverage passes 132
tests; result-artifact coverage passes 19 on Windows and the same 19 on WSL
Ubuntu/Python 3.11.9.  Ruff, compilation and whitespace checks pass.  A new
hosted head is still required before merge.

## 2026-07-17 - Product update lock ownership correction

PR run `29524461343` verified the connector correction and again passed Windows
x64 plus macOS arm64/x64, but correctly stopped promotion after one Ubuntu
failure with 2,180 tests passed and 35 skipped.  The failing audit-lifespan test
exposed a product startup race rather than audit corruption: the newly started
update poll read `current_release_identity` on one worker thread while the
Runtime readiness recorder called `mark_runtime_ready` on another.  Both use
the same `InstallCoordinator` and `ProductFileLock`; the old implementation
treated any different-thread owner as immediately unavailable even when the
product intended to wait.  Cross-runner byte stability was skipped and no
release or production switch occurred.

`ProductFileLock` now reserves a single in-process acquisition and uses a
condition variable.  The owning thread retains re-entrancy, while another
thread waits according to one deadline covering both the in-process and OS
lock phases.  A zero timeout remains fail-fast; the production update
composition explicitly selects `timeout=None` so its trusted background
workers serialize instead of failing the Runtime lifespan.  Ownership is not
transferred until backend unlock and descriptor close finish.  Backend or
stream exceptions always clear the acquisition reservation/owner state and
wake waiters, preventing a failed cleanup from permanently wedging updates.

The regression uses observable condition barriers, not sleeps.  It fixes an
identity reader inside the critical section, proves the readiness recorder is
waiting, then releases the first owner and verifies both operations complete
without dual ownership.  Finite timeout, non-owner release, backend failure,
stream-close failure, re-entrancy and process exclusion remain covered.  On
WSL Ubuntu with exact Python 3.11.9, update composition plus durability pass all
23 tests; the focused lock/product barrier set passes all 18 tests.  The
implementing agent additionally repeated the deterministic race 20 times and
the thread stress 250 times.  Ruff, compilation and whitespace checks pass.
Independent review found no remaining P0/P1.  A new hosted full matrix is still
mandatory before merge.

## 2026-07-17 - Protected Stage and pre-signing contract closure

Hosted run `29526376684` passed the protected PR matrix at head `a1cc16c8`:
Ubuntu quality/deterministic build, Windows x64, macOS arm64/x64 and cross-runner
byte stability were all green.  PR #12 was squash-merged under repository
protection as exact main `de70b480f20acc1b5f19b740e67f6282f33037f8`.
The first exact-main platform Stage, run `29526938093`, then failed safely before
creating any signed platform input.  Both macOS runners exposed the same GA
test-helper lifetime defect: three assertions read selected security/Cookie
headers through a delayed closure over `IncomingMessage` after response
teardown.  Windows installed the exact uv-managed Python base correctly, but
then tried to modify that PEP 668 protected interpreter, so Packaging and
Playwright were unavailable.  No failed-run file is reusable.

The GA helper now snapshots the complete raw on-wire header block inside the
HTTP response callback.  Exact Node 22.23.1 passes all 180 Web tests and the GA
file passes three consecutive isolated runs.  Windows Stage now creates a
disposable venv under `RUNNER_TEMP` from the exact non-registry Python 3.11.9
base; it never uses `--break-system-packages`.  A fresh local reproduction
installed all 53 hash-locked platform-stage packages, imported
Packaging/Playwright/NumPy/ONNX Runtime and passed dependency-lock validation.
The workflow gate asserts this isolation contract.

The pre-signing audit found and closed three additional production defects
before they could be embedded in immutable signatures.  First, Candidate
assembly had placed `stable` inside the CDN URL while the production replica
serves `/ecorex-agent/releases/v1.0.0/<release_id>`.  Recipe, Candidate source
validation and replica now share that canonical URL; a real
recipe-to-signed-manifest-to-upload/finalize integration passes.  Second, the
mutable Bootstrap freshness pointer was inside the root-owned read-only site
slot.  It now lives at the Control-Plane-owned
`/srv/ecorex-agent-download/public-pointer/public-bootstrap-index.json`, with
one exact Nginx/Caddy route.  Legacy/current aliases are atomically removed.
The immutable site tree excludes this object, while the deployment
authorization still binds its initial digest and immutable target.  Root
readback verifies the release-key authority signature and the distinct
publication-key freshness signature, rejects unknown/tampered/expired keys or
target/source drift, and permits only a valid freshness renewal.  CP restart
CAS renewal preserves mode 0644 and does not change the signed slot.

Third, v0.2.9.2 Admin/identity import had been an external operator step rather
than part of cloud activation.  First activation now requires explicit
`legacy_admin_migration.source_version=0.2.9.2`, freezes both writer sets,
records a fixed cutoff and source/snapshot/Admin-receipt/identity digests in
the activation journal, commits the idempotent Admin and identity imports, and
uses the Admin receipt as authority for the commit-before-journal-fsync crash
window.  Before any target write, deterministic failure restores legacy;
after a target receipt, recovery is monotonic roll-forward and never starts a
second legacy writer.  Dry-run now executes the real read-only target preflight
and validates all public/secret environment dependencies.

Unified WSL Ubuntu/Python 3.11.9 regression passes 179 cloud, migration,
pointer, public-site, Control Plane and reproducibility tests.  The canonical
CDN integration set passes four tests; exact Node 22.23.1 Web passes 180/180.
Ruff, compileall, source-tree (753 files), dependency locks, Runtime/server
schema authorities, legacy cutoff, public-download gate and whitespace checks
pass.  A new protected PR matrix, merge and wholly new exact-main platform
Stage remain mandatory; no production traffic or public release was changed.

## 2026-07-17 - First-route continuity and canonical CDN closure

The final pre-commit audit found that the first cloud deployment could retire a
legacy exact Bootstrap route before the Control-Plane-owned pointer existed.
It also found a seed-to-route-retire window in which a legacy publisher could
change the source after the first copy.  A live read-only check confirmed the
actual old site still returned 200 for `/ecorex-agent/`, Basic-Auth 401 for
`/ecorex-agent/admin/`, and 404 for the not-yet-existing public Bootstrap
pointer.  No live bytes or routes were changed by that check.

The cloud deployer now seeds and binds a typed pointer identity before template
installation.  If a legacy exact route exists, it uses a stable inode/size read
and validates either the strict unpublished schema or both independent release
authority and publication-freshness signatures.  If the old site has no exact
route and no pointer, it creates only the canonical `unpublished` document;
it never turns missing legacy state into a fabricated release.  Immediately
before changing the Nginx server file it repeats the stable source/target read,
schema/signature validation and exact payload/length/SHA-256 comparison.  Any
route, source or target drift stops before config mutation or reload.  Template
failure and Candidate compensation preserve the seeded bytes and old authority.

The public CDN contract is now uniformly
`/ecorex-agent/releases/v1.0.0/<release_id>/<asset>`.  Channel remains internal
replica storage derived from the signed release ID; cloud Nginx, the public
Nginx example and Caddy map that canonical URL to
`v1-artifacts/v1.0.0/<channel>/<release_id>/<asset>` and reject all directory or
unmatched paths.  The Stage venv is uniquely bound to run ID, attempt and target
and refuses any pre-existing directory.

Final local exact-Python regression passes 193 tests with 12 explicit platform
skips; the complete Candidate/CDN/public set passes 40 tests; exact Node
22.23.1 passes all 180 Web tests.  Independent review reports no remaining
P0/P1/P2.  Live/CDP/model/image acceptance remains explicitly `WAIVED`, not
passed.  A new protected PR matrix and wholly new exact-main Stage are still
required before signing or production mutation.

## 2026-07-17 - Exact-main Stage runtime-source and header-snapshot correction

GitHub Actions billing was restored and PR #13 run `29531945083`, attempt 3,
passed the complete five-job matrix.  PR #13 was squash-merged as exact main
`c042e4a3997e8289bd24b33ae600a2ba5b249a4c`.  Read-only production drift
verification still found the v0.2.9.2 site and services authoritative, the
legacy SQLite database healthy and unchanged, and no premature v1 slot,
pointer, keyring or systemd activation.

Protected exact-main Stage run `29541415646` then failed closed and produced no
reusable success artifact.  On Windows the new isolated Stage venv exposed a
second-order packaging defect: the Python closure copied `sys.executable`,
which was the venv launcher and required an external `pyvenv.cfg`.  The signed
closure probe therefore exited 106 with `No pyvenv.cfg file`.  The stager now
selects the real versioned interpreter, standard library and DLL closure from
one resolved `sys.base_prefix`, rejects symlink/reparse/prefix escape and
stable-reads the interpreter at the copy boundary.  Regression tests prove a
venv launcher is never selected and preserve the macOS versioned-base contract.

Both macOS Stage jobs independently exposed a Node response-header projection
defect in the GA helper.  The three failed assertions read CSP and Set-Cookie
as empty even though the server emitted them.  The client now snapshots the
Node 22 `headersDistinct` projection synchronously inside the response
callback, retains a raw-wire fallback, preserves repeated Set-Cookie values and
returns only immutable-copy accessors after response teardown.  A dedicated
loopback test verifies case-normalized CSP and two distinct cookies after the
socket lifecycle ends.

Local correction evidence uses stable Python 3.11.9 and exact Node 22.23.1:
the complete platform-pack staging file passes 52 tests with one explicit
platform skip; Ruff and Python compilation pass; WebUI passes 181/181 tests.
The failed Stage remains quarantined.  A new protected PR matrix, new exact-main
merge and wholly new same-run three-platform Stage are mandatory before direct
admission, signing or production mutation.

## 2026-07-17 - Cross-shell Platform Stage fail-fast correction

Forensic review of exact-main Stage run `29541415646` found that Windows had
also reported the same three GA Web-test failures seen on macOS. They did not
stop the Windows job: GitHub Actions used PowerShell for the multi-line `run`
block, a native `npm run test:v1` non-zero status was followed by a successful
`npm run build`, and the step therefore appeared successful. The later Python
closure failure became the visible Windows terminal error. Those three GA
failures were real and are not reclassified as passed.

Both multi-command Stage blocks now delegate to one repository-owned Python
runner with fixed command catalogs and no shell command interpolation. It
executes sequentially, streams each child directly to the job log, and returns
immediately with a non-zero status on launch failure or the first failed child;
no later command can overwrite that status on PowerShell. Bash keeps its
normal `-e -o pipefail` workflow wrapper, while both shells now observe the
same single runner exit code. A real subprocess regression proves that a first
command exiting 23 prevents a later successful command from running, and a
workflow contract prevents the vulnerable multi-line blocks returning.
The dependency-lock gate now treats that indirection as a new pinned trust
boundary: it requires the three exact workflow bindings once each, rejects any
adjacent inline dependency/build command, compares the AST literal catalog to
all nine reviewed child commands and pins the complete executable runner AST.
Missing, duplicate, argument-drift and execution-bypass mutations all fail.

## 2026-07-17 - Hermetic Web dist build-before-test correction

Fresh Stage run `29544524231` proved fail-fast was working on all three Stage
platforms, but also exposed an ordering defect in the newly controlled catalog:
`npm run test:v1` ran before the only production Web build. A clean checkout
has no `desktop/dist`, so the GA test correctly stopped with `dist_missing` on
Windows x64 and both macOS architectures. No failed Stage artifact was reused.

The Stage first runs a cross-platform Python `clean-check` after the fixed
Python toolchain is ready and before dependency installation. It rejects a
pre-existing `desktop/dist` and any Git porcelain output without depending on
Bash on Windows. The audited Web group is exactly six commands: `npm ci`,
typecheck, one build, test, content-address validation and the read-only bundle
gate. Stable tree digests before and after test and after both validators must
match. The runner also compares all six resolved commands (executable, argv,
cwd and order) with the audited catalog, so truncation or reordering fails
closed. The dependency-lock expected catalog and complete runner AST pin move
with this closure.

CI and Candidate now give the workflow sole ownership of the one production
Web build. They seal a canonical byte contract immediately after that build,
run Web, Playwright and Python suites against it, then seal and compare a
second contract. Any direct or indirect test-side mutation of `desktop/dist`
fails before packaging can consume it. The real Web release contract no longer
invokes Vite; destructive rehashing uses a temporary copy and a `finally`
guard proves the production tree stayed byte-identical even when an
intermediate assertion raises.

## 2026-07-17 - Browser driver mode and release secret-scan closure

Protected Stage runs after the portable macOS Browser-signature work exposed
two independent boundaries. First, distribution closure copying normalized
Playwright's private POSIX `driver/node` to `0644`; Playwright executes that
file directly, so Browser startup could not cross the driver boundary. Browser
staging now validates the exact pinned driver path and establishes `0755`
before signing, inventory and ZIP binding. PR #30 and its exact-main CI both
passed all five jobs, and the next fresh Stage crossed the Browser functional
smoke.

That Stage then found semantic drift between Stage and Candidate secret scans:
Stage applied text-token regular expressions to signed opaque Browser bytes,
while Candidate already limited those detectors to canonical text/config
members. A shared release scanner now owns both gates. Complete PEM private
keys remain blocked in every payload; AWS/GitHub/Slack token shapes are scanned
from raw bytes for the fixed text/config path contract, including malformed
UTF-8 and NUL-containing text. Opaque members remain protected by exact locks,
tree binding, architecture and portable signature checks. Failed runs
`29585694303` and `29588232914` are quarantined and cannot feed Candidate or
publication.

The next non-disclosing Stage locator refined that boundary. The remaining
complete-PEM match was not in Browser or product configuration: its location
and content hashes mapped exactly to the original, hash-locked macOS arm64
`opencv-python==5.0.0.93` `libgnutls.30.dylib` member. Secret-shape detection is
therefore now defined by canonical text/config paths for every detector,
including PEM. It still scans raw bytes, so malformed text cannot evade the
gate. Opaque native members are admitted only through the stronger applicable
controls: exact wheel hashes, tree/content binding, target architecture,
relocation and portable signatures. This restores the Candidate scanner's
historical native/text boundary while keeping Stage and Candidate on one
implementation.

## 2026-07-17 - macOS Seatbelt evidence-complete probe

Fresh Stage `29596154340` crossed the corrected Browser and native payload
boundaries, then failed at the macOS sandbox behavioral probe. The denial was
real: after proving that the child could not write outside the workspace, the
probe attempted to read the protected canary again from inside Seatbelt and
turned the expected denial into an unhandled process failure. Its fixed closed
loopback port could also return `ECONNREFUSED` without any sandbox policy.
That run is quarantined and supplies no release evidence.

The corrected probe separates trusted host evidence from untrusted sandbox
evidence. The host creates a random canary and a live random loopback listener;
the sandbox reports exact errno values for direct read/write, child write and
network attempts. Readiness requires only `EACCES` or `EPERM`, an exact
successful child report, a no-follow regular marker created before the child
attempt, an unchanged host canary and exact typed JSON keys. Crashes, missing
files, I/O errors, connection refusal, booleans masquerading as integers,
malformed values and extra or absent evidence all fail closed. The real macOS
test is now a release assertion instead of an environmental skip.

The first protected Stage after that correction exposed one remaining macOS
network-boundary detail: Seatbelt can reject `socket()` itself, before
`connect_ex` can return an errno. The live-listener design remains authoritative,
but socket construction and connection are now one explicit `OSError` boundary.
Only `EACCES` or `EPERM` passes the unchanged strict evaluator; connection
refusal, success and unrelated errors still fail closed. Stage `29598702668`
was cancelled on its first platform failure and is fully quarantined. To stop
future generic failures from forcing inference, each remaining evidence branch
now has a fixed non-disclosing reason code. Stage emits only an explicit
allowlist of those codes and never emits errno values, paths, commands or
provider output.

The classified Stage then exposed `macos_seatbelt_probe_process_unavailable`.
The bounded process was not necessarily unavailable: host validation opened
the child marker before evaluation, and any missing/refused marker raised into
an outer catch that replaced already captured process and JSON evidence with
`None`. Canary and cleanup errors had the same evidence-collapse shape.
Captured subprocess evidence is now immutable. Host checks translate only to
their own fixed failure facts, and cleanup can never replace a primary result;
`process_unavailable` is reserved for the actual bounded runner boundary.

The next classified Stage reached the bounded sandbox process but returned
`macos_seatbelt_probe_process_nonzero`. The only remaining unstructured
operations inside the probe were child-process launch and socket cleanup.
Both now produce typed evidence: child launch must report exact zero before a
return code is accepted, and a constructed socket must close successfully
without replacing the already captured denial errno. Provider text, paths and
raw errno values remain private; Stage receives only fixed allowlisted codes.

Even after structuring those operations, Stage still reported a non-zero
sandbox process. A return code cannot prove whether the relocated interpreter
executed any probe code. The probe now flushes one constant startup handshake
as its first action and requires exactly one canonical JSON line after it.
Every later operation is inside a top-level fixed-phase envelope. Future
failures therefore distinguish interpreter startup, missing handshake and the
precise in-probe phase without exposing stderr, exception text, host paths,
arguments or errno values.

## 2026-07-18 - Truthful macOS workspace-write semantics

The startup handshake isolated the remaining Stage failure to the Seatbelt
read policy rather than Python or probe logic. A deny-default list of selected
Framework paths cannot productize the dynamic read closure of a signed Python
distribution. More importantly, it did not match the user-visible
workspace-write profile: this profile permits reads, scopes writes to chosen
workspaces and denies network.

The macOS backend now implements and behaviorally proves that exact contract.
Read evidence is a digest match for a host canary, never its content; direct
and inherited outside writes remain denied, workspace writes succeed and the
live network listener remains unreachable. The runtime no longer publishes a
false `workspace-only` read claim: sandbox probes carry a canonical read scope
which is bound into the Pack contract id. Invocation TEMP/TMP also lives in a
private hidden directory under the selected workspace, so normal office tools
can use temporary files without receiving broader write authority. The
directory is removed only after process cleanup on every terminal path.

PR #38 and exact-main run `29606908819` passed the protected matrix (the
quality job required one failed-job rerun after its first attempt timed out).
Stage `29609800335` is quarantined and cannot feed Candidate or publication.

The next fresh Stage (`29612323015`) crossed the interpreter and complete
Seatbelt behavioral probe, validating the corrected workspace-write contract.
It then reached the later Bootstrap build and returned the formerly generic
`bootstrap_test_failed`. The bounded Go test runner now consumes Go's JSON
event stream and maps only exact source-owned test identities to fixed public
codes. Multiple, unknown/package and process-boundary failures remain distinct
and fail closed; stderr, arbitrary output, host paths and toolchain details are
never emitted. The failed Stage is quarantined in full.

Stage `29614152095` then proved that more than one source-owned Bootstrap test
failed. A multi-test result now carries only a sorted set of fixed allowlisted
public test codes plus a bounded count. `StageError` independently validates
that contract and drops any arbitrary value, duplicate, path-shaped content or
out-of-range count. This preserves the non-disclosure boundary while allowing
the next clean Stage to identify the shared product cause. The entire failed
run remains quarantined.

The following Stage (`29615646417`) generated the fixed multi-test set inside
the stager, but the digest-pinned parent adapter still forwarded only the
older secret-scan hash diagnostic. The adapter now independently validates a
closed copy of the public Bootstrap code set, sorted uniqueness, exact fields
and bounded count consistency before forwarding. Raw tests, Go output, stderr,
paths and malformed values remain private. The failed run is quarantined.

Stage `29617026723` then identified the exact shared Bootstrap failures:
pointer authority persistence, pointer freshness persistence and trusted local
migration configuration. All three first call the install-root link guard.
On hosted macOS, Go's test temp path is exposed through a system alias whose
resolved canonical path differs, so the tests constructed an install root the
product correctly rejects. The harness now resolves and validates a real,
non-link canonical directory before invoking the security contract. Product
link rejection is unchanged and the failed Stage remains quarantined.

PR #43 and exact-main run `29617969135` passed the protected matrix. Stage
`29618345433` then completed the macOS arm64 adapter successfully, but its
success path wrote a human progress marker to stdout before the one allowed
JSON protocol response. The strict parent correctly rejected that mixed
stream as `platform_stager_response_invalid`; the entire run is quarantined.
The marker is removed rather than weakening the parser, and a static AST gate
now reserves stdout for the single fixed success response while requiring all
`print` calls to target stderr explicitly.

PR #44 and exact-main run `29619217940` passed the protected matrix. The next
Stage (`29619611874`) crossed the corrected protocol, then failed while the
parent generated receipts because receipt hashing applied raw credential
regexes to every opaque native byte stream. The stager's supply-chain gate had
already passed the same tree using the centralized path-aware policy. Receipt
generation now reuses that policy and its bounded text-contract scope while
preserving streaming hashes and stable-file identity checks. Actual secrets in
text contracts remain rejected; token-shaped Mach-O/native bytes no longer
produce a false credential result. The failed run remains quarantined.

## 2026-07-18 - Product-owner release-chain deferral

The product owner explicitly directed EcoreX to skip the current protected
PR/Stage/signing/publication loop after it had consumed disproportionate time.
This was a temporary workflow decision, not a Stage pass and not a release
exception that permits publication. It was superseded later in the same
operator session by an explicit instruction to deploy after verification. The
resumed path must still use the repository's controlled direct-admission or
normal Candidate authority; it may not reuse a failed Stage artifact or turn a
waived live check into a passed check.

The local branch at this checkpoint is `codex/fix-ga-turn-duration`.
`bbea1da9` corrects the GA mock server so user-accepted and terminal Turn
timestamps are live rather than a historical fixture time, preventing an
acceptance-only elapsed-time inflation. The user-owned `.artifacts/` tree
remains untracked and untouched.

The resumption authorization does not by itself prove any remote mutation.
Production remains unchanged until the authenticated release authority accepts
the exact immutable release evidence and the post-activation Web/Admin readback
succeeds.

## 2026-07-18 - Direct-production continuation: model-gateway admission hold

The direct Stable candidate was published through the signed release boundary,
and the server-side Cloud sidecar was advanced only as far as its durable
pre-route migration journal.  The legacy Web/Admin/API services were restored
and remain the active public route while this journal is incomplete; neither
the new WebUI nor `/admin/` has been claimed as live.

The v0.2.9.2 administrator migration preserved 40 live users, excluded seven
deleted users, retained eligible conversation/project data independently, and
imported six encrypted model drafts.  Deleted conversations are not revived.
The retained-key revalidation flow created auditable new revisions and made
real catalog/inference probes without exposing a key, endpoint credential,
prompt body, response body, or generated image.

Only the existing Doubao chat slot passed and is active.  The primary
`gpt-5.6-sol`, Gemini, Image 2 and Image 2 Edit probes correctly remain
inactive because their fixed HTTPS bridge upstreams are unreachable from the
production host; the retained DeepSeek revision returned an incompatible
provider response contract.  The historic OpenAI/Gemini endpoints are
public-HTTP-only addresses, so v1 intentionally will not send a retained key
to them.  This is a release hold, not a failed-open fallback.

The production Control Plane and Gateway origin maps now include the existing
loopback TLS Image bridge.  Final Cloud and public Web/Admin activation remains
blocked until a reachable, trusted HTTPS upstream (or an equivalently secure
operator-managed egress path) is supplied for the primary/image providers and
passes the same real activation probe.

The product owner subsequently supplied the missing authority: retain the
historic public-HTTP provider addresses. The bridge now has a distinct
root-owned public-HTTP waiver which accepts only a pinned global IP literal.
It does not permit hostnames, redirects, Admin-supplied origins or arbitrary
paths; the loopback-facing side remains TLS and exposes only the fixed model
routes. The focused bridge and sidecar regression passes 93 tests with seven
explicit Windows/platform skips.

The exact `74800e60` source was then installed as the production bridge
authority. Its root-owned spec now pins the historic OpenAI/Image and Gemini
global-IP origins under that waiver; validation, `nginx -t`, graceful reload
and every loopback TLS route probe passed as one rollback-capable operation.
The retained-key workflow subsequently activated GPT-5.6 SOL, DeepSeek and
Doubao. Gemini's legacy proxy reached its inference route but returned HTTP
502, so that revision remains rejected rather than being advertised.

Both Image 2 operations reached the legacy provider and returned valid
base64-encoded PNG result fields when the compatibility request explicitly
selected `b64_json`. The activation request previously omitted that selector,
so the provider was reachable while the strict v1 validator correctly
classified its alternate result as a protocol failure. The activation
contract now requests `b64_json` for both generation and edit without relaxing
PNG, dimensions, count, size, status, content-type, redirect or digest checks.
The focused model-activation and management suite passes 22 tests.

The follow-up binary-header diagnostic found the second provider-specific
contract mismatch: the approved legacy proxy ignores the requested 1024px
square and returns a valid 1254px square while declaring `size=auto`.
Arbitrary dimensions remain rejected. For the Image 2 slots only, a native
square within one-half to two times the requested edge is now admitted by the
activation probe and normalized inside the bounded image worker to the exact
requested dimensions. The adapter requires identical aspect ratio, PNG input,
decoded-memory and byte limits, and revalidates the normalized PNG before CAS
publication. The expanded activation, management and direct-provider suite
passes 42 tests.

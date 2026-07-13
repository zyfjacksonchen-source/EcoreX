# EcoreX v1.0 architecture decision log

## ADR-001 - Backend is the only product authority

- Status: accepted.
- Decision: React renders versioned backend projections and submits typed user
  intents. Model selection, permissions, routing, artifacts, queues, updates,
  connectors, and terminal state are owned by the local Python runtime or cloud
  control plane.
- Consequence: legacy localStorage and extension-based business decisions are
  migration inputs only, never v1 sources of truth.

## ADR-002 - Append-only events precede delivery

- Status: accepted.
- Decision: Thread/Turn/Item changes and job transitions are committed before
  SSE or other projections are emitted. Each thread has a monotonic sequence.
- Consequence: the WebUI can use one deterministic reducer and reconnect from a
  watermark without synthesizing terminal state.

## ADR-003 - Office artifact visibility is explicit

- Status: accepted.
- Decision: every artifact has family, role, visibility, actions, revisions, and
  lineage. Source code, scripts, diffs, logs, temporary files, and diagnostics
  are internal and never enter the user artifact projection.
- Consequence: the user-facing "show implementation files" control is removed;
  frontend extension blacklists are deleted after backend integration.

## ADR-004 - Permission profile is immutable per execution snapshot

- Status: accepted.
- Decision: default means workspace-write plus on-request approval. Full access
  means danger-full-access plus approval-never; only an administrator hard deny
  can block it. Non-permission business input and connector authentication still
  use persistent interaction requests.

## ADR-005 - Install and update are one transaction coordinator

- Status: accepted.
- Decision: first install and upgrades share a cross-process lock, immutable
  version/digest pin, download verification, side-by-side slots, journal,
  healthcheck, and rollback. Activation remains user-confirmed.

## ADR-006 - Web-only product, cryptographically signed releases

- Status: accepted.
- Decision: Electron and native application signing/notarization are retired.
  Runtime bundles and manifests remain signed and hashed because they execute on
  the user machine.

## ADR-007 - One build, one runtime/web digest pair

- Status: accepted.
- Decision: React is built exactly once per release. The runtime manifest pins
  its Web bundle digest; overlay patches and copied runtime source mirrors are
  not release inputs.

## ADR-008 - UI system is quiet, tactile, and token locked

- Status: accepted.
- Decision: the product uses a single clipped workspace surface, complete corner
  radii, tonal elevation, no persistent-card shadows, semantic OKLCH tokens, and
  accessible hover/focus/touch action patterns. Feature CSS may not invent
  colours, radii, shadows, or z-index values.

## ADR-009 - Capability routing is a recorded backend pipeline

- Status: accepted.
- Decision: every Turn receives an immutable capability snapshot produced by
  `Catalog -> Availability -> Governance -> Ranking -> Exposure`. Semantic
  intent changes rank only and cannot delete unrelated eligible tools or grant
  invocation authority. An exact, affirmative user-selected tool alias may be
  promoted to direct exposure after the same availability and governance
  checks. Unknown references remain recorded and fail closed.
- The intent policy targets reviewed semantic facets and effects such as
  `media.image.create + generate_media`, never a concrete tool ID. A catalog
  replacement that implements the same reviewed contract can therefore win the
  route without changing the planner. The built-in image capability is merely
  the current highest-ranked eligible implementation; routing never invokes it
  automatically.
- Image availability is composed from the signed pack/catalog, managed model
  modality, provider/runtime health and current policy before exposure. An
  image intent ranks the eligible media implementation first in deferred
  discovery while leaving read, fetch, vision, browser and shell discoverable.
  A missing pack, offline service or administrator deny remains authoritative.
- Consequence: the model progressively discovers deferred tools through
  `tool_search` and exact `tool_describe`; only a completed durable describe
  fact grants that tool to the same Job/Thread/Turn and frozen capability and
  permission snapshots. Invocation checks the grant, tool version, provider
  revision and policy again immediately before side effects. The React client
  never submits an authoritative list of enabled tools. This follows the useful Codex separation between
  capability gating, model-visible tool specs, deferred discovery and runtime
  dispatch while keeping EcoreX's routing facts deterministic and replayable.
  Clean-room reference: OpenAI Codex
  [`spec_plan.rs`](https://github.com/openai/codex/blob/bbdf3030dec1e7894cbe58051076ea66d2c9208f/codex-rs/core/src/tools/spec_plan.rs)
  and
  [`registry.rs`](https://github.com/openai/codex/blob/bbdf3030dec1e7894cbe58051076ea66d2c9208f/codex-rs/core/src/tools/registry.rs).

## ADR-010 - Model aliases resolve only inside a managed modality catalog

- Status: accepted.
- Decision: chat, image, vision, audio, and embedding catalogs are distinct
  backend projections available at bootstrap. User/model aliases resolve to a
  canonical managed model ID before a Thread exists; no arbitrary provider or
  key can be introduced through the client.
- Consequence: image selectors cannot drift from backend support and aliases
  such as `image2` cannot accidentally be interpreted as a chat model.

## ADR-011 - Artifact ownership and delivery are separate facts

- Status: accepted.
- Decision: Artifact metadata carries immutable account/thread/turn ownership.
  User projection checks ownership and visibility; delivery uses a durable
  leased outbox and a stable event ID before appending to a Thread stream.
- Consequence: binary storage, Artifact transactions and conversational events
  may recover independently without leaking another account or double-emitting
  a feedback/retouch fact.

## ADR-012 - Job context is relational, not user payload

- Status: accepted.
- Decision: Turn snapshot IDs are stored in immutable
  `job_runtime_contexts`, not inside the public Job payload.
- Consequence: workers can replay the exact policy/capability context while UI
  clients cannot forge or observe implementation-only payload fields.

## ADR-013 - v1 WebUI is the only production renderer

- Status: accepted.
- Decision: `AppV1` is imported directly by the sole Vite entry. The legacy
  App, global CSS monolith, Electron source, native packaging/signing scripts
  and their dependencies are not build inputs.
- Consequence: production can no longer select an old bundle through a query
  parameter or overlay; the signed Web manifest is the exact allowlist.

## ADR-014 - Model discovery and model execution health are separate

- Status: accepted.
- Decision: `/bootstrap` always exposes the backend-owned managed model catalog
  before the first message, while a separate `model_service` projection states
  whether a managed gateway is configured. A durable Agent worker pool exists
  only when that gateway is injected into the Runtime lifecycle.
- Consequence: UI selectors no longer depend on a conversation or provider
  secret, an offline service cannot masquerade as ready, and local history and
  Artifact operations continue independently. In-flight work is recovered by
  job leases and checkpoints rather than browser retries.

## ADR-015 - User permission preference is mutable; Turn policy is immutable

- Status: accepted.
- Decision: default/full-access is an account-scoped durable preference owned by
  the Runtime. Each accepted Turn captures the effective preference plus
  administrator hard-denies as a new immutable snapshot. Active Turns are not
  silently upgraded or downgraded when the preference changes.
- Consequence: full access persists and remains visible/revocable, admin denies
  cannot be bypassed, retries cannot restore stale authority, and Replay can
  explain the exact policy used by every tool invocation.

## ADR-016 - Production Web assets are named by final bytes

- Status: accepted.
- Decision: Rollup hashes are staging markers only. A fail-closed post-build
  dependency pass rewrites all references bottom-up, names assets by the first
  16 hex characters of final SHA-256, and admits the exact allowlisted tree to
  the signed Web bundle manifest.
- Consequence: Runtime/Web digest binding is independently verifiable and a
  source edit cannot leave a stale production filename. Cyclic emitted chunks
  are rejected instead of receiving a non-content hash fallback.

## ADR-017 - Connector credentials and lifecycle are backend-only

- Status: accepted.
- Decision: the React client may request a Connector action but cannot submit
  credentials, redirect URIs, provider policy or authoritative health. A
  versioned backend definition/instance/adapter contract owns OAuth PKCE,
  credential-vault references, action schemas, leases and recovery.
- Consequence: Connector availability can change between Turns without UI
  drift; each future Turn records the current backend snapshot. The single
  OAuth callback exemption is exact-path GET only and retains loopback Host,
  one-time state, PKCE, expiry and fencing protection.

## ADR-018 - Bootstrap owns process switching; Runtime owns update intent

- Status: accepted.
- Decision: the Runtime downloads, verifies and stages a release, but a small
  independently signed Bootstrap supervisor owns slot validation, known-good
  selection and process restart. Activation requires an explicit idempotent
  user request and revalidates the currently eligible rollout before restart.
- Consequence: a paused rollout or kill switch can stop activation even after
  download, the browser never launches binaries, and a failed new slot can
  return to a cryptographically verified known-good slot.

## ADR-019 - Managed model retries have logical and attempt identities

- Status: accepted.
- Decision: the cloud Gateway binds and replays one immutable request ID, while
  the local Runtime derives that ID from Turn, durable Job attempt and model
  round. A retry scheduled by policy increments the Job attempt and therefore
  receives a new cloud request identity.
- Consequence: a duplicated HTTP stream cannot double-execute or double-charge,
  an uncertain crashed call is not repeated, and a later authorized retry does
  not become trapped replaying the prior terminal failure.

## ADR-020 - WSS is a hint; signed feed is activation authority

- Status: accepted.
- Decision: `update.available` only wakes the client. Download and activation
  eligibility always come from a fresh authenticated, signed HTTPS feed. The
  client forbids WSS redirects and verifies TLS hostname; activation compares
  the entire staged manifest and repeats signature verification.
- Consequence: a lost WSS message is recovered by polling, and a forged/stale
  hint cannot select a package or bypass pause/kill-switch policy.

## ADR-021 - Thread history is a backend projection with signed pagination

- Status: accepted.
- Decision: task navigation reads `/api/v1/threads` ordered by committed event
  time. Its opaque keyset cursor is HMAC-bound to the status filter and has one
  canonical encoding. Rename/archive/restore are evented, idempotent mutations;
  stale retries return current state and never roll newer state backward.
- Consequence: the React sidebar can switch projections but cannot invent
  history, ordering, titles or lifecycle. Mock Replay and catalog state share
  the same event-defined timestamps.

## ADR-022 - Public shares and private diagnostics are different products

- Status: accepted.
- Decision: a `ShareSnapshot` is an immutable, expiring, revocable public
  projection with a unique backend ID and cloud token. It contains messages and
  public Artifact metadata only. A `DiagnosticSnapshot` has a separate ID,
  schema and storage path, contains redacted event metadata only, and can never
  receive a public URL.
- Consequence: thumbs-down cannot accidentally publish a conversation, two
  Threads cannot reuse one link, internal files/tool payloads remain private,
  and clipboard success is a client observation rather than share creation.

## ADR-023 - Connector lifecycle commands have durable request identity

- Status: accepted.
- Decision: authentication, health, disconnect and reauthorization bind a
  stable client request ID to one fingerprint and durable terminal result.
  Reauthorization switches credential references only after the new OS-vault
  entry exists and records old-entry cleanup for restart recovery.
- Consequence: retries and concurrent clicks cannot repeat provider effects or
  destroy a working connection halfway through credential rotation.

## ADR-024 - Release promotion is one resumable administrator transaction

- Status: accepted.
- Decision: the administrator client records one stable request ID for candidate,
  every required gate, publication, rollout creation and activation in an
  atomic local journal protected by a cross-process lock. Credentials are read
  from the environment at call time and never enter arguments or the journal.
- Consequence: routine releases use one product command, an ambiguous network
  failure is safely replayed, and a rerun cannot silently create a second
  rollout or bypass a required gate.

## ADR-025 - Retouch execution is a unified Durable Job, not an Artifact side queue

- Status: accepted.
- Decision: a structured retouch request, its Artifact job and one Runtime
  Durable Job are created atomically. The managed call has a stable external
  idempotency key and recovery endpoint; only the supervised Runtime worker may
  commit a result revision and public Turn item.
- Consequence: restart cannot double-edit an image, the page never manufactures
  an imagegen prompt, completed edits appear with the actual image/change/check
  evidence, and an unconfigured service fails before creating an orphan queue.

## ADR-026 - A public share needs keyed state integrity, not only an opaque token

- Status: accepted.
- Decision: the cloud stores only a hash of the public token, authenticates each
  snapshot lifecycle state with a keyed MAC, and chains administrative lifecycle
  audit entries with HMAC. Runtime publication is HTTPS/443, non-redirecting and
  bounded; every public payload is a server-built allowlist projection.
- Consequence: possession of a database row does not reveal its URL token,
  cross-account source IDs do not collide, and state/audit tampering or deletion
  fails closed without exposing internal conversation or Artifact data.

## ADR-027 - Capability availability means a verified executable handler

- Status: accepted.
- Decision: `installed_packs` is derived only from an Ed25519-verified pack whose
  platform, Runtime API, artifact digest and exact backend `ToolSpec` digest all
  match. A tool without a bound callable is explicitly disabled in the immutable
  Turn snapshot. Tool inputs and outputs are validated on both sides of the
  handler boundary.
- Consequence: an image-intent ranking boost cannot erase sibling capabilities,
  but neither can a catalog entry or stale pack flag make a non-existent tool
  appear usable. Packs provide dependencies/adapters; they cannot inject a new
  contract or arbitrary code into the core.

## ADR-028 - A managed session is signed cloud authority, not local configuration

- Status: accepted.
- Decision: account, organization, roles, model allowlist, quota and admin
  denies come from a maximum-72-hour Ed25519 lease. Token plaintext is held only
  by the OS vault and rechecked against signed commitments on every use. A
  binding change requires a controlled Runtime restart.
- Consequence: the WebUI and local config cannot mint access, expand models or
  weaken administrator policy; expired identity stops mutations/model calls
  without making local history unavailable.

## ADR-029 - Retouch is a first-class operation Turn

- Status: accepted.
- Decision: every structured retouch creates one new backend Turn and one
  dedicated Durable Job in the same transaction as its Artifact request. The
  Turn receives a freshly captured authoritative snapshot and never creates an
  `agent_turn` Job.
- Consequence: source-image history is immutable, replay and UI phases are
  honest, concurrent retries converge, and stale policy cannot leave a partial
  edit queue behind.

## ADR-030 - A capability pack is signed twice in the release graph

- Status: accepted.
- Decision: a pack has its own domain-separated Ed25519 sidecar binding its
  exact ToolSpec digests and dependency ZIP. Both files are also immutable
  release artifacts bound to the product release identity.
- Consequence: a pack can be verified and activated independently, while a
  mirror/CDN cannot mix it across releases or substitute a handler contract.

## ADR-031 - Admin refresh restores one database snapshot

- Status: accepted.
- Decision: the administrator page restores candidates, explicit latest IDs,
  rollouts, channel kill switches and distribution from one WAL read
  transaction behind the same release-admin authorization as mutations.
- Consequence: refresh/restart cannot lose the release workflow, and a
  concurrent rollout change cannot produce a mixed half-old/half-new console.

## ADR-032 - Session binding changes restart the verified slot

- Status: accepted.
- Decision: login, logout and signed account/policy binding changes request
  Runtime exit `86`. The Bootstrap supervisor re-verifies and restarts the same
  immutable release slot. Update activation alone uses exit `85` and requires a
  newly selected signed slot.
- Consequence: a live process cannot mix repositories, workers or permissions
  from two accounts, while session changes do not impersonate a product update
  or rebuild Web assets.

## ADR-033 - First login is a vault-backed device flow

- Status: accepted.
- Decision: the local WebUI receives only a flow ID, user code, HTTPS
  verification URL and status. The provider device secret and managed tokens
  remain in the OS vault/broker/session boundary; only a verified signed lease
  can populate identity, model, quota or administrator policy.
- Consequence: EcoreX can start as an unauthenticated local shell and offer a
  usable first-login path without accepting API keys, tokens or cloud policy
  from React or process arguments.

## ADR-034 - Public share side effects are supervised jobs

- Status: accepted.
- Decision: create and revoke commit a Share command and one Durable Job in the
  same transaction. Only the leased Share worker may call the provider and
  commit the terminal URL/revocation; its payload contains no share content or
  provider secret.
- Consequence: a closed tab or Runtime crash cannot strand an unowned
  `publishing`/`revoking` row, concurrent retries reuse one external identity,
  and a late publish cannot defeat expiry or revocation.

## ADR-035 - Product code runs only from a Bootstrap-verified slot

- Status: accepted.
- Decision: the public `ecorex` command is the v1 Product server and accepts
  only host/port. Runtime configuration is a canonical signed Core member; the
  process must be launched by Bootstrap from the selected slot and re-verifies
  Core/Web identity before constructing any provider client.
- Consequence: user machines never git-pull, npm-build or pip-compose a live
  Runtime, process listings carry no cloud credentials, and an old overlay or
  alternate bundle cannot silently become the product UI.

## ADR-036 - External Artifact actions are at-most-once backend commands

- Status: accepted.
- Decision: React submits only an Artifact identity, declared action and
  idempotency identity. Runtime authorizes and materializes the CAS revision,
  persists a launch receipt and treats a crash after launch begins as an
  unknown terminal outcome that requires an explicit new command.
- Consequence: local paths and platform process details never become frontend
  authority, retries cannot repeatedly open applications, and replay remains
  deterministic without re-running desktop side effects.

## ADR-037 - Optional cloud transports exist only through signed Product config

- Status: accepted.
- Decision: Share, Retouch, Audit and managed Connector transports are composed only when the
  canonical signed Core configuration names their exact HTTPS/443 route and
  host allowlist. They all consume the same managed session authority and have
  one explicit process-lifecycle owner.
- Consequence: a UI preference, environment variable or redirect cannot create
  provider reachability, change account scope or leak an orphaned transport;
  disabling a service is represented truthfully as `null` at composition time.

## ADR-038 - Disclosed reasoning is a durable replaceable Item

- Status: accepted.
- Decision: only provider-approved reasoning summaries enter the public Runtime
  as `reasoning` Items. A stable atom ID owns monotonic revisions. The first
  visible delta of a new atom emits one `reasoning.replaced` fact that archives
  the prior atom and creates the new visible Item in the same transaction.
  Ordinary Turn, tool and message facts cannot alter its presentation;
  terminal collapse requires an explicit ordered `reasoning.archived` fact.
- Consequence: a summary cannot flash away between model/tool phases, event
  reconnect and Mock Replay reconstruct the same visible revision, and React
  remains a projection rather than a timer-based owner of reasoning state.

## ADR-039 - Stable office connectors use one managed adapter boundary

- Status: accepted.
- Decision: Feishu and Tencent Docs are the only stable v1 adapters accepted by
  signed Product configuration. Their OAuth relay, health, actions and revoke
  calls use connector-specific fixed paths below one HTTPS/443 managed gateway.
  The returned provider grant is opaque and lives only in the OS vault.
- Consequence: React never receives third-party tokens or provider API
  authority, connector lifecycle/idempotency/scope/replay remain local backend
  facts, and Beta directory entries cannot silently become executable adapters.

## ADR-040 - Legacy source may be migrated but cannot enter a v1 product artifact

- Status: accepted.
- Decision: the v1 Python distribution includes only `ecorex*`, while Core/Web
  release scanners independently reject old CLI, WebChannel, chat HTML,
  Electron and overlay content. Migration readers are the only intentional
  boundary to v0.3.0 data formats and are read-only.
- Consequence: preserving a dirty v0.3.0 workspace or migration fixture cannot
  accidentally make legacy runtime code executable in a signed v1 install;
  packaging and dependency graphs fail the release if that boundary regresses.

## ADR-041 - Precise-retouch canvas state is a versioned backend draft

- Status: accepted.
- Decision: React may coalesce pointer gestures briefly, but a retouch workspace
  is a SQLite projection keyed by account, Artifact and immutable base revision.
  Every save and submit uses an expected workspace version. The edit surface
  binds the base revision, raster digest, oriented pixel dimensions, EXIF
  orientation, color space and coordinate-space version. References pin their
  Artifact revisions and are limited to ten.
- Consequence: refresh, process restart and slow projection delivery cannot
  discard or silently rebase annotations. The image worker receives structured
  geometry and pinned identities rather than a frontend-composed prompt or
  filesystem path; stale base/reference revisions fail before external work.

## ADR-042 - Public release visibility is fenced by every signed origin

- Status: accepted.
- Decision: the low-level GitHub uploader may create and resume only a draft.
  The Product publication coordinator verifies the immutable local release once,
  finalizes the signed domestic mirror first, uploads the GitHub draft second,
  finalizes the signed CDN third and may only then make GitHub public. Every
  remote receipt URL, byte length and SHA-256 must match the signed manifest;
  credentials are resolved from named environment variables at request time.
  Control Plane publication requires separate GitHub, mirror and CDN gates.
- Consequence: an administrator cannot accidentally publish a GitHub-only
  release whose first download source is empty, a stale script cannot overwrite
  a same-name asset, and retrying the one publication command resumes the same
  release identity without exposing tokens in arguments or configuration.

## ADR-043 - Bootstrap alone confirms a provisional Runtime activation

- Status: accepted.
- Decision: the old Runtime may drain, dry-run migration, persist an activation
  intent and atomically switch the current pointer, but it must stop in
  `healthchecking`. The non-known-good candidate is launchable only by a
  dedicated Bootstrap verifier when its exact signed Release/Artifact,
  transaction journal, prior pointers, Core payload, Web bundle and storage
  identity match the durable intent. Bootstrap passes a one-use health nonce
  through the child environment and confirms known-good only after an exact
  loopback proof. The nonce is never placed in argv, config, journal or a
  database.
- Consequence: a static file check in the old process can no longer bless code
  that never started. The candidate is a probe-only ASGI process that opens no
  business database or Provider and rejects every normal mutation. Pre-data
  failure, including failure of the confirmed full Runtime's first process,
  restores the signed prior pointers through a replayable receipt. Prior slots
  are retained until the confirmed full Runtime records its storage barrier;
  after that point recovery is roll-forward-only.

## ADR-044 - Browser and sandbox packs execute behind a bounded child protocol

- Status: accepted.
- Decision: a signed browser/sandbox pack is a canonical Python zipapp with an
  embedded descriptor that must exactly match its outer signed tool bindings.
  Core starts one isolated child per invocation, passes the backend-owned
  permission/workspace snapshot over bounded canonical JSON, strips parent
  credentials and user-controlled executable search paths, bounds both output
  streams and time, and validates the correlated response before publication.
- Consequence: a pack exception, malformed response or output flood cannot
  crash or exhaust the Runtime process, installed-pack flags cannot fabricate
  a handler, and shell/CDP authority remains fail-closed unless a release-signed
  executable pack implements the exact Core contract. Platform sandbox E2E is
  still mandatory evidence for each signed pack build.

## ADR-045 - Public Share keys rotate by persisted verification identity

- Status: accepted.
- Decision: the Control Plane Share keyring has one active issuance key and a
  bounded set of retired verification keys. Every new public token, immutable
  snapshot state MAC and append-only audit entry persists its non-secret key ID
  and MAC version. Rotation changes only the active key; an existing snapshot
  continues to derive the exact same URL and uses its original key for state
  validation and revocation. Pre-keyring rows remain MAC v1 and are annotated
  transactionally with an explicitly selected legacy key identity; new rows use
  key-bound MAC v2.
- Consequence: routine rotation cannot invalidate existing links, expiry or the
  audit chain, while an unknown or prematurely removed historical key fails
  closed. Key IDs cannot be rewritten because they are part of immutable row
  identity, key material never enters a URL/projection/database, and ambiguous
  legacy upgrades are rejected instead of guessing which key signed old data.

## ADR-046 - OTLP traces export immutable terminal segments through a durable outbox

- Status: accepted.
- Decision: Product tracing uses dependency-free OTLP/HTTP JSON with proto3
  JSON field mapping at an allowlisted HTTPS/443 `/v1/traces` endpoint. A Turn
  terminal event records that Turn segment in the same SQLite transaction; a
  Thread archive records the root segment. The backend projects, redacts,
  bounds and AES-GCM encrypts deterministic batches before a leased dispatcher
  sends them with the exact managed-session account and an idempotency key.
  Partial success with rejected spans is a terminal diagnostic, never success.
- Consequence: active spans are not emitted prematurely and complete Thread
  snapshots are not repeatedly uploaded. Runtime failure can cause only an
  idempotent resend after lease expiry, not loss of a committed terminal
  segment. Trace encryption keys remain local under the existing audit-key
  authority; signed configuration and React contain endpoints and limits only.

## ADR-047 - Replay eligibility and execution remain backend projections

- Status: accepted.
- Decision: the task Header More menu opens one thin diagnostics surface. A
  successful Mock Replay response is the integrity-verification authority and
  supplies its watermark, event count, digest, reconstructed projection and
  exact `live_replay_turn_ids`. React does not infer Live eligibility from Turn
  status because a fork may display inherited terminal Turns that the current
  task cannot execute. Live Replay remains a separate confirmed mutation: the
  user must select a backend-authorized source and check an explicit warning;
  retries preserve one `client_request_id`, current permissions are replanned,
  and historical external side-effect results are never reused. Capability
  availability and governance stay orthogonal: a missing signed pack makes a
  tool ineligible/hidden but does not erase the immutable
  `requires_approval` decision captured from current policy.
- Consequence: viewing diagnostics cannot call a model, tool, connector or
  external writer. A failed/ambiguous Live response cannot create a second
  Turn on retry, and an accepted response refreshes the authoritative task
  projection so the new Replay Turn appears in the ordinary timeline instead
  of a frontend-only result state. Installing a pack can change availability,
  but cannot retroactively rewrite the Replay-visible governance decision.

## ADR-048 - Control Plane update hints use a durable shared signal log

- Status: accepted.
- Decision: rollout activate/pause/halt and channel kill/clear mutations append
  bounded signal facts in the same Control Plane database transaction as their
  canonical state and administrator audit. Signals contain only monotonic
  sequence, stable event identity, operation, channel and release/rollout
  identity; account/organization targets, actor identity and credentials never
  enter the payload. Every app instance has a stable consumer identity, a
  persisted monotonic cursor and a bounded asynchronous poller. Delivery is
  intentionally at least once: a crash after local WSS fan-out but before cursor
  acknowledgement repeats the same event ID. Time retention always preserves a
  configured latest floor. A cursor gap processes only the retained committed
  suffix; facts already removed by retention recover through the existing
  periodic signed feed, which remains the final authority. The Hub verifies an
  exact sequence-and-field match against the durable row and has no API that
  can mint rollout or resync event identity.
- Consequence: Control Plane processes sharing the product database propagate
  rollout hints without an in-memory-only hub or mandatory Redis dependency.
  Tenant targeting is re-evaluated from canonical rollout state immediately
  before local WSS delivery. Pause/halt hints wake only clients that matched the
  affected rollout and still cannot authorize an install; the client must
  observe the signed feed, where a revoked rollout returns no update. A missed,
  duplicated or retention-pruned hint therefore changes latency only, never
  release selection or activation authority.

## ADR-049 - Legacy executable source is history, not a v1 rollback mechanism

- Status: accepted.
- Decision: the tracked v0.3 WebChannel tree, `chat.html`, copied Web bundles,
  overlay patches, Electron sources and their release packagers are removed
  from the v1 source tree. Source-era launchers fail immediately with exit 78;
  production starts only a Bootstrap-verified `ecorex.server` slot. A static
  release gate rejects reintroduction and rejects any `ecorex` import of the
  legacy `agent`, `channel`, CLI or provider graph. The migration package may
  parse inventoried v0.3 data formats but cannot import or execute old code.
- Consequence: rollback uses the updater's signed, side-by-side known-good
  slots rather than a mutable source mirror. Git history, ignored local build
  caches and read-only migration samples may remain for diagnosis, but none is
  a Core/Web release input and none can silently revive the old Runtime.

## ADR-050 - Extensions share one durable authority and one Turn-time revocation fence

- Status: accepted.
- Decision: Skill, MCP server, tool provider, connector provider and Capability
  Pack declarations enter one SQLite-backed Extension registry. Revision
  identity is derived from the unsigned canonical manifest and artifact digest;
  detached signature rotation adds immutable, re-verifiable evidence without
  changing that identity. Core declarations are bound to the Bootstrap-verified
  build. Publisher/admin declarations require Ed25519. A `local_bundle` may be
  only a declarative Skill: ZIP and administrator-selected directory inputs are
  normalized into a canonical, per-file SHA-256 inventory and content-addressed
  snapshot. `local-content-sha256` proves local content integrity, never
  publisher trust. Root `SKILL.md` frontmatter accepts only name, description,
  version, license, Runtime compatibility and tags; executable/script/hook/bin,
  command/env/secret/network/native namespaces and formats are rejected. MCP
  providers must use the stable `2025-11-25` protocol and exact registered
  export IDs. A migrated `legacy_import` is metadata only and can never be
  enabled or runtime-bound.
- Consequence: catalog, enable/disable, health, quarantine, dependency closure,
  rollback and migration are backend facts rather than independent Skill/MCP/UI
  switches. Every accepted Turn captures an immutable
  `extension_snapshot_id` beside configuration, capability and permission
  snapshots; its Job/events and Live Replay preserve that identity. Immediately
  before a real tool invocation, Runtime requires the same provider revision to
  remain enabled, healthy, dependency-complete and currently provenance-valid.
  Disabling, quarantining, replacing or revoking its signature/CAS therefore
  stops old queued Turns as well as new capability exposure. React only uploads
  a bounded ZIP or submits a projected action with expected revision and
  idempotency identity; it never accepts a host path, loads code or invents
  availability.

## ADR-051 - The public download page is an untrusted, atomically published discovery surface

- Status: accepted.
- Decision: the checked-in public pointer is permanently safe to deploy before
  GA because its only repository state is canonical `unpublished` with a null
  release and no URL/signature. A release job may replace it only after reading
  the exact signed manifest bytes, matching their SHA-256 to release metadata
  and one immutable three-origin publication receipt, verifying the manifest
  and all Windows x64/macOS arm64/macOS x64 Bootstrap signatures, and passing
  the strict v1 discovery schema. Replacement uses the product file lock, a
  same-directory fsynced temporary file and atomic rename. Mutable HTML/index
  responses are `no-store`; JS, CSS, images and release assets are named by
  content digest and served immutable. Before rendering links, the browser
  retries the manifest sources in signed order and checks the exact response
  bytes against the projected SHA-256.
- Consequence: an interrupted deploy cannot expose a partial pointer, a stale
  proxy cannot pin mutable discovery, and the source tree cannot accidentally
  advertise a fabricated “ready” release. The browser digest check remains a
  corruption/self-consistency check, not signature authority; only Bootstrap's
  embedded Ed25519 trust store may authorize installation or update.

## ADR-052 - Runtime consistency is one transaction graph, not five local state machines

- Status: accepted.
- Decision: Event, Turn, Item, Durable Job and Interaction projections are
  audited from one SQLite WAL read snapshot against the immutable fact stream,
  reference scope, snapshot/trace identity, lease fencing tuple and terminal
  dependent rules. The auditor reports bounded non-sensitive codes and never
  repairs evidence. SQLite connections override the standard library's
  transaction-breaking `executescript` behavior: a script invoked inside an
  owned transaction executes complete statements without an implicit commit
  and cannot issue transaction control. Job storage also rejects partial lease
  tuples at the database boundary. When a lease expires, a retryable provider
  or tool phase enters `retry_wait` in the same transaction as `job.reclaimed`;
  exhausted attempts atomically fail the Turn/Items/HITL; a committed
  `model.response_completed` in `finalizing` atomically completes even on the
  last attempt. Recovery Turn facts causally reference the Job fact that made
  the decision. A Turn's configuration, capability, permission, Extension and
  trace snapshot identities cannot be replaced by a later event.
- Consequence: a process crash cannot publish half of a state/audit mutation,
  dead-letter a Job while leaving its Turn permanently streaming, or retry a
  response whose terminal fact already committed. The invariant report makes
  corruption observable without silently rewriting it. SQLite work performed
  by Agent and Update background loops is offloaded from the ASGI event loop,
  so WAL contention delays only that operation rather than SSE keepalives,
  worker heartbeats and every connected UI.

## ADR-053 - System health is a bounded Runtime projection, not a collection of UI guesses

- Status: accepted.
- Decision: the local Runtime owns one low-cardinality signal registry and one
  bounded SQLite health history. It samples event-loop lag, active/peak SSE
  connections, emitted facts, process resources, database/WAL size, queue age,
  Turn/Job/HITL/image/retouch state, Artifact/Memory volume and aggregate
  Worker/Connector/Extension/Update health. Collection runs outside the ASGI
  event loop, provider failures become explicit degraded facts, retention is
  capped, and only health transitions enter the append-only audit trail. The
  primary `/api/v1/system/health` contract contains plain-language component
  status and no raw metric payload; authenticated technical details and bounded
  history are separate, opt-in projections. Provider values pass through the
  same path/secret redaction boundary used by audit.
- Consequence: the UI can explain whether EcoreX is responsive, queued,
  storage-constrained or partially degraded without inspecting implementation
  state or inventing diagnoses. Metrics cannot grow the business database
  without bound, block task execution, upload Artifact binaries, or expose
  credentials and host paths. SSE lifecycle counters are maintained at the
  stream boundary, while React polls a small status projection and loads
  technical JSON only when the user expands “技术详情”.

## ADR-054 - Output locations are immutable Turn policy, not browser download preferences

- Status: accepted.
- Decision: the Runtime maps the closed aliases `documents`, `downloads` and
  `workspace` to backend-owned absolute roots; React can select an available
  alias but can never submit or receive a host path. One account-bound Output
  authority shares the Runtime/Artifact SQLite database and records every
  preference revision, immutable policy snapshot, idempotency fact,
  materialization state and audit fact. Turn admission resolves the current
  policy and embeds `output_policy_snapshot_id` in the immutable config
  snapshot. Saving a user-visible Artifact follows its persisted scope through
  `turn.accepted → config_snapshot_id → output_policy_snapshot_id`; only a
  migration-era Artifact with no Turn may use the current preference.
  Materialization verifies the Artifact CAS stream, rejects internal/source/
  script/diff/log families, leases and revalidates the selected root, publishes
  through a same-directory fsynced exclusive link and recovers the durable
  `preparing → published → completed` state after a crash.
- Consequence: changing the default affects only later Turns, so a long task
  cannot switch directories halfway through. Concurrent equal content reuses a
  verified file; different content receives a deterministic revision/digest
  suffix and never overwrites an existing entry. A replaced directory, symlink,
  reparse point, stale policy, cross-account request or mismatched Runtime/
  Artifact database fails closed. The UI reports the alias and display name
  after “保存到默认位置” without learning the host path.

## ADR-055 - CI compatibility evidence cannot authorize a release

- Status: accepted.
- Decision: v1 pull requests and `main` run a read-only GitHub Actions quality
  job plus explicit Windows x64, macOS arm64 and macOS x64 compatibility jobs.
  Python 3.11, Node 22 and the dev tools are pinned. Every runner builds the
  same content-addressed WebUI and emits one canonical, timestamp-free byte
  contract covering identity JSON, v1 shell sources, HTML and digest-named
  JS/CSS. A final job requires the Ubuntu/Windows/two-macOS contracts to be
  byte-identical. Git attributes force LF for shell, JSON, JavaScript, CSS,
  Python, TOML and workflow sources so Windows checkout policy cannot change a
  signed input, including the generated HTML entrypoints. CI has only
  `contents: read` and receives no release secrets.
- Consequence: a pull request can prove lint, tests, source compatibility and
  deterministic bytes without acquiring publication authority. A green CI run
  is not an Ed25519 release signature, SBOM/license/secret scan, installed
  Runtime drill or three-origin publication receipt; those remain separate
  immutable-candidate gates and cannot be synthesized from CI status. Native
  application code-signing/notarization is outside the WebUI product scope.

## ADR-056 - Browser rendering is bounded work, not a mirror of storage size

- Status: accepted.
- Decision: the WebUI may retain complete backend projections for deterministic
  replay, but it cannot turn projection size into unbounded network or DOM
  work. Media previews load only near the viewport through one revision-aware,
  abortable LRU with explicit byte/entry/concurrency ceilings. The chat DOM
  starts with an anchored 120-message window and pages history without allowing
  new deltas to move an older page. Streaming text/reasoning deltas coalesce at
  most once per animation frame with a bounded background fallback; terminal,
  permission, interaction and other state facts flush synchronously. Async
  dialog loads carry a UI request fence in addition to the Runtime/thread
  fence.
- Consequence: a task with many messages or media artifacts cannot allocate one
  object URL/request/DOM subtree per stored record, and a late response cannot
  overwrite newer user intent. The authoritative Event/Artifact stores remain
  complete; browser pagination and cache eviction affect rendering cost only,
  never task state, replay identity or the downloadable original.

## ADR-057 - The release console is served by the Control Plane, not the download site

- Status: accepted.
- Decision: the public Bootstrap site is an untrusted, static discovery surface
  and contains no administrator implementation. Its `/admin/` link and the
  `/api/v1/admin/*` namespace terminate at the v1 Control Plane, which serves
  verified content-addressed assets and authenticates every mutation. The
  public proxy has no route to a user's loopback Runtime. The legacy static
  admin, separate SQLite Admin API, usage panel, Basic Auth state, old client
  channel keys and `/message`/`upload`/port-9909 routes are retired inputs.
- Consequence: candidate gates, rollout percentage, pause/halt/kill switch,
  distribution and WSS hints have one transactional authority. Publishing a
  download-site directory cannot create or mutate rollout state, and restoring
  an old static folder cannot bypass the Control Plane's role, idempotency,
  signature or audit checks.

## ADR-058 - Legacy execution state is evidence until the user reauthorizes it

- Status: accepted.
- Decision: v0.3.0 run snapshots/events, queue payloads, schedules and permission
  files are copy-on-write migration inputs, never v1 execution authority.
  Completed terminal facts may enrich a matching imported Turn. Active/queued
  message work becomes a redacted recovery draft, schedules remain disabled,
  and remembered grants/filesystem rules remain unbound. They can become live
  only through an explicit v1 interaction after current account, policy,
  connector, model and capability snapshots exist. Branch lineage is written
  only when child/parent request ownership proves it. Source identity records
  released-schema compatibility separately from package-marker metadata because
  the historical v0.3 release evidence does not identify one consistent
  archive-to-commit chain.
- Consequence: starting v1 after migration cannot replay hidden context, resume
  an obsolete lease, send a connector message, invoke a tool, run a schedule or
  inherit an unreviewed host path. The user's recoverable intent is retained,
  malformed or contradictory state fails staging, and the untouched v0.3 source
  remains the rollback authority until v1 activation succeeds.

## ADR-059 - A shared image is snapshot data, not a local Artifact URL

- Status: accepted.
- Decision: ShareSnapshot v2 contains a bounded immutable media descriptor and
  Turn association, never a local path, blob URL, base64 body or unsigned
  public location. The durable local worker reads verified Artifact CAS bytes,
  uploads media under an account/share/media idempotency identity and only then
  publishes the immutable JSON snapshot. The Control Plane authenticates MIME,
  magic, size and SHA-256, links only declared media in the same publish
  transaction and resolves it only through the active snapshot token. Four
  upload slots and item/share/account orphan limits bound memory and disk;
  unlinked old bytes are reclaimable while published links are append-only.
- Consequence: the public HTML and its images cannot drift into different
  lifecycles. A missing upload never produces a broken public link, and revoke,
  expiry, cross-account lookup or undeclared-media guessing cannot retrieve the
  bytes. Old schema-v1 snapshots remain byte-compatible and render with the
  same chat language but without fabricated media.

## ADR-060 - Conversation order is Event sequence, never wall time or opaque ID

- Status: accepted.
- Decision: Runtime projection queries derive first Turn/Item order from the
  immutable per-thread Event `seq`; fork replay already follows that stream.
  React preserves the order supplied by a projection and appends newly
  persisted events in delivery sequence. Timestamps remain display metadata,
  while ULIDs and other IDs remain identity only.
- Consequence: frozen clocks, same-millisecond imports and intentionally opaque
  IDs cannot put an Agent reply before the user's instruction, change the
  active Turn chosen by the UI or make replay disagree with live rendering.

## ADR-061 - Ordinary controls reveal framing through interaction state

- Status: accepted.
- Decision: ordinary text, icon, navigation and tool controls have transparent
  border/background and no shadow at rest. Hover, `focus-visible` and active
  states reveal one low-contrast surface and 1px boundary; primary, dangerous,
  selected and input controls keep their semantic persistent treatment. System
  UI/monospace stacks and the 14/22, 13/20, 12/16 scale are global Tokens, and
  a CI contract rejects broad font shorthands or feature CSS that re-boxes an
  ordinary control.
- Consequence: the interface gains Codex-like information density without
  invisible keyboard state or undersized touch targets. Visual hierarchy comes
  from spacing and surface lightness instead of every row becoming a card.

## ADR-062 - Runtime validates storage; signed candidates migrate it

- Status: accepted.
- Decision: ordinary Runtime startup may create the complete current schema
  only when the SQLite database contains no user tables. Every non-empty
  database must carry the exact compiled storage version and the canonical core
  table/index/trigger fingerprint. Missing metadata, columns, indexes,
  triggers, or a same-name altered definition fails before Runtime DDL. Cross-
  version evolution is admitted only through the candidate's Ed25519-bound,
  declarative migration plan, using the same plan hash for copy-on-write dry
  run, live preflight, activation and receipt verification.
- Consequence: a restart cannot silently turn pre-GA drift or corruption into a
  partly upgraded database. Current-schema creation, signed migration,
  pre-data rollback and post-barrier roll-forward have distinct evidence and
  authority.

## ADR-063 - Cloud image concurrency requires shared leases and shared CAS

- Status: accepted.
- Decision: multi-worker image generation and retouch use PostgreSQL 15+ as the
  shared lease/state authority and S3-compatible content-addressed storage as
  the shared byte authority. PostgreSQL may not be paired with node-local CAS.
  Worker slots derive from the process memory envelope; database pools, upload
  chunks, item/share limits and request semaphores are all bounded. Blob create,
  reference mutation, tombstone and deletion use digest/ETag compare-and-set so
  a crash or retry cannot overwrite or delete a different object.
- Consequence: scaling out does not create duplicate execution, node-affine
  previews, unbounded upload memory or unsafe garbage collection. SQLite/local
  CAS remains a deterministic single-node correctness mode; real PostgreSQL/
  S3 load and outage recovery remain release-environment gates.

## ADR-064 - One compiled catalog owns the complete local Runtime schema

- Status: accepted.
- Decision: the local Runtime schema is the deterministic union of the core
  Event/Job tables and 17 non-optional domain fragments. A fresh database
  creates that union in one `BEGIN IMMEDIATE` transaction and stores its
  canonical whole-catalog digest. Every Repository validates its fragment and
  the catalog before business DML; constructors cannot create, alter, drop or
  repair objects. Feature availability changes rows and projections, never the
  physical schema. An AST gate rejects local Runtime DDL outside the catalog,
  signed migration engine and one-time import boundary.
- Consequence: startup order and feature flags cannot produce different
  databases, and a missing or weakened trigger cannot be silently restored in
  a way that hides corruption. Pre-GA same-version drift now needs an explicit
  signed source fingerprint and migration instead of a column-presence guess.

## ADR-065 - A signed candidate binds the target physical schema, not only a number

- Status: accepted.
- Decision: storage-migration manifest schema v2 requires a lowercase SHA-256
  of the complete target SQLite schema. ReleaseBuilder emits and validates that
  digest against the compiled Runtime catalog; admission dry-run, live
  preflight, activation and durable receipts independently compare the actual
  post-migration digest. Schema-v1 plans have a separate test-only parser and
  are unconditionally rejected by every Product and activation path.
- Consequence: two layouts carrying the same integer version cannot pass as the
  same release. A signed but incomplete plan, altered index/trigger or legacy
  manifest fails before traffic moves to the candidate.

## ADR-066 - Local Runtime, Control Plane and Gateway have separate import and migration authorities

- Status: accepted.
- Decision: importing a narrow Runtime storage module must not execute the
  application/worker/capability/update graph. Public composition and worker
  exports are lazy, and Capability Pack verification imports the exact update
  contract modules rather than the aggregate package. Cold subprocess tests
  cover both import orders. Independently deployed Control Plane core and Model
  Gateway SQLite services use their own version/checksum history, exclusive
  migration lock, complete object fingerprint and explicit migrate/validate
  command; their repositories open an existing database read/write and only
  validate at construction.
- Consequence: process startup no longer depends on which public package was
  imported first, and a client release cannot accidentally migrate a server
  database. Unknown/future/tampered server layouts fail closed instead of being
  opportunistically repaired by the first replica to start.

## ADR-067 - Audit integrity is incremental on the hot path and complete on demand

- Status: accepted.
- Decision: each Control Plane and Cloud Audit repository performs one full
  chain verification at construction, then keeps a thread-safe
  `(sequence,digest)` or `(sequence,MAC)` checkpoint. A transaction rechecks
  the checkpoint tail and verifies only later append-only rows. The checkpoint
  advances only after commit; rollback cannot advance it. If a post-commit
  in-memory merge observes an impossible equal-sequence conflict, the committed
  operation still returns success and the repository becomes poisoned so the
  next operation fails closed. Only an explicit successful full verification
  clears poison. Full integrity export validates and materializes rows in one
  scan rather than validating and querying twice.
- Consequence: a 4 Hz signal poll or ordinary audit query no longer scans the
  complete lifetime ledger. A committed mutation is never misreported as a
  retryable failure, while tail gaps and new-row corruption remain immediate
  blockers and older-history tampering remains detectable through the explicit
  operator audit.

## ADR-068 - Public Share media uses reference metadata and an opaque object CAS

- Status: accepted.
- Decision: the Control Plane database stores immutable media identity,
  digest, MIME, object key, reference count, release tombstone and access
  metadata, never the media bytes. A token-authorized response opens an opaque
  verified object handle and streams a bounded byte range with ETag; local
  single-node storage caps simultaneous file streams, while production may
  inject a shared object implementation. Revocation and expiry release a
  reference and delete only a zero-reference tombstoned object. The one known
  legacy BLOB layout is migrated in two phases: short read transactions prepare
  and verify CAS objects plus a canonical checkpoint outside any exclusive
  lock, then a short exclusive finalize revalidates the source/checkpoint,
  switches metadata and commits immutable schema/media receipts.
- Consequence: two snapshots can safely deduplicate one image, revoking one
  cannot delete the other's bytes, and service restart or object-store failure
  cannot expose a half-published snapshot. Legacy object I/O does not hold the
  Control Plane database write lock, and an unknown historical layout cannot be
  accidentally adopted as the current schema.

## ADR-069 - Image service storage validates a complete physical catalog

- Status: accepted.
- Decision: both the single-node SQLite reference store and production
  PostgreSQL image store are initialized only by explicit deployment migration
  commands. Their immutable receipts bind a fixed source catalog, migration
  checksum and complete target catalog digest. PostgreSQL canonicalization
  includes table flags, ordered column type/null/default/identity/generated/
  collation properties, PK/unique/FK/check constraints, index method/keys/
  opclasses/order/nulls/include/predicate, trigger enablement/timing/events/
  condition/function/arguments, function attributes and body digest, owned
  sequences and extra managed inventory. Runtime stores open read-only schema
  validation transactions and never execute DDL. A source gate permits DDL
  only in seven exact server migration modules.
- Consequence: a replica cannot bless a partly initialized PostgreSQL database,
  a same-name weakened constraint or trigger body, or an unexpected managed
  object. The local fake-catalog suite proves the comparison logic, while the
  real PostgreSQL 15+ catalog/deparser result remains an explicit environment
  test and cannot be inferred from SQLite or mocks.

## ADR-070 - Update WSS fan-out evaluates clients in bounded snapshots

- Status: accepted.
- Decision: one update hint batch contains at most 1024 validated client
  identities. The Repository verifies the durable signal and related rollout/
  release once in one transaction, updates activation/clear client heartbeats
  in bulk, evaluates organization/account/percentage/minimum-version/platform
  eligibility per client, and caches manifest/artifact signature verification
  only inside that snapshot. The Hub validates all targets before database
  work, processes bounded batches, and enqueues only after every batch succeeds
  while rechecking that each connection is still current.
- Consequence: 500 online clients require one transaction and 2000 require two,
  rather than one transaction plus repeated signature verification per client.
  An invalid target produces no partial hint, a disconnected client cannot
  receive a stale event, and a slow bounded queue replaces an older hint rather
  than growing without limit.

## ADR-071 - Built-in Control Plane production is explicit single-node SQLite WAL

- Status: accepted for the built-in v1 provider; PostgreSQL/HA remains open.
- Decision: the production CLI composes the existing core, Cloud Audit and
  Cloud Share schema/repository authorities as one single-node SQLite WAL
  service. A persistent-volume marker, process-held cross-platform lock,
  verified online backups and `replica_count=1` make that limit executable.
  Share media must use a private encrypted S3 bucket; Cloud Audit encryption/
  integrity keys and Share keyrings arrive only through a narrow secret-provider
  seam. Identity and release trust use bounded Ed25519 public-key rings. `serve`
  validates and never migrates. PostgreSQL or multiple replicas fail before
  storage opens, while a typed production-provider protocol reserves the future
  HA composition boundary.
- Consequence: a missing database, wrong volume, second process, local object
  fallback, stale/corrupt backup, public/unencrypted bucket, bad key, low disk or
  failed update poller blocks readiness. SIGTERM drains HTTP/WSS before resource
  release, and raw access logs stay disabled because Share tokens occur in URL
  paths. The built-in service can be deployed honestly today, but cannot be
  called HA; PostgreSQL schema/repository and real multi-node evidence remain a
  named GA extension rather than an implicit SQLite claim.

## ADR-072 - Candidate signing accepts only attested platform trees and stdin-only external signatures

- Status: accepted for signing authority; its twelve-tree/three-Pack topology is
  superseded by ADR-082.
- Decision: source CI remains credential-free and read-only. A separate
  protected platform-stage dispatch uses digest-pinned self-hosted tooling to
  produce three real WebUI Runtime trees and browser/image/sandbox Packs for
  every target. Each tree carries a content digest, exact target, source
  commit, workflow-run identity and fixed platform gate receipts. Candidate
  assembly accepts exactly those 12 trees from a successful same-repository,
  non-PR dispatch of the same commit. ReleaseBuilder remains the only archive,
  Pack sidecar, SBOM and manifest constructor. Its signer is an executable/
  adapter pair pinned by SHA-256; payloads use stdin, signatures use bounded
  stdout, and the configured public key verifies every response. Publication
  and Control Plane activation are distinct explicit environment-approved
  operations; defaults are no publication, dry-run and 1%.
- Channel namespace: stable keeps the public GitHub tag `v1.0.0`; each canary
  uses `v1.0.0-canary-<build-prefix>`. Mirror and CDN roots end in
  `/v1.0.0/stable` or `/v1.0.0/canary`, then append the immutable `release_id`.
  Source roots and the scoping mode participate in `build_digest`, while final
  URLs are derived only after that digest exists. This prevents a canary draft
  or prior canary build from owning the immutable tag/object prefix needed by
  another Candidate or the formal release.
- Consequence: neither a PR nor a green source test can access signing/origin
  credentials. A missing Windows helper, browser/image adapter, platform
  Runtime closure, staging receipt, protected signer or origin credential
  stops the chain without a fake artifact. User packages contain built bytes
  and never assemble themselves with git/npm/pip. GA still requires real
  protected-runner, KMS/HSM, install/update/rollback and three-origin outage
  evidence; local deterministic fixtures prove contracts, not infrastructure.

## ADR-073 - Human interaction is a versioned durable contract, not arbitrary UI JSON

- Status: accepted.
- Decision: every persisted interaction carries a v1 typed form/action contract
  and accepts the exact `{action_id, values}` response shape with a required,
  durable client request ID. Runtime validates kind-specific actions, declared
  fields/options, required and length constraints, and rejects credential
  fields or credential-shaped values before writing facts. Connector login is
  action/status-only. Tool-driven follow-ups are admitted only through a key
  explicitly declared in the immutable ToolSpec output schema.
- Consequence: React maps one backend contract; Event Replay and restart recover
  the same form and response; retry is payload-fenced; Worker can suspend after
  a completed tool and resume without repeating its side effect. Adding a new
  form control, response value type or connector-login action requires a new
  contract version rather than a frontend-only convention.

## ADR-074 - Intent routing is trusted evidence, not a tool-name branch

- Status: accepted.
- Decision: the Capability Planner has no media vocabulary and no concrete
  image tool identity. It evaluates a versioned `IntentRoutingPolicy` injected
  by product composition against bounded, product-reviewed semantic facets on
  `ToolSpec`. A rule declares required effects/facets, exact normalized positive
  and suppression phrases, and a bounded score boost. Product semantic routing
  does not grant direct exposure; only an exact affirmative explicit tool
  selection may do so after availability and governance checks.
  The v1 built-in policy recognizes image creation and editing separately and
  requires `GENERATE_MEDIA`; a future reviewed replacement can carry the same
  facet without preserving the `imagegen` ID. Policy may come only from Core or
  signed trusted product configuration. The external MCP contract cannot set a
  routing facet or score, so names, descriptions, effects and free-form search
  tags cannot self-authorize priority. Rule count, phrase count/length, facet
  count/length and score are all hard bounded. Multiple matching rules use the
  strongest boost rather than stacking user text or metadata repetition. An
  exact eligible tool reference always ranks above a non-explicit semantic
  route. Image-model aliases are route evidence only when generation/edit
  action language is also present; a model question, price query or fault
  report is not an image action.
- Decision trace: every immutable `CapabilityDecision` records its final
  candidate score, matched catalog/explicit/route evidence and availability,
  governance, catalog or intent suppression reasons. `CapabilityPlan` binds
  the routing policy ID, version and digest, and the durable snapshot repository
  round-trips those fields for restart and Replay. A negated or diagnostic use
  of an explicit media alias remains evidence but is not an unconditional tool
  invocation. Availability, Pack presence, connectivity and policy are still
  evaluated independently; routing can change rank/exposure only and can never
  make an ineligible tool callable.
- Consequence: strong Chinese/English generation or reference-image editing
  ranks the eligible reviewed media capability first in deferred discovery,
  while read, fetch, vision, browser and shell remain direct/deferred candidates.
  Image inspection, generation/edit failures, routing/architecture/model
  discussions, explicit “do not generate/edit” requests and office documents
  that merely contain images do not promote it.
  A changed vocabulary/policy produces a new immutable plan identity instead
  of silently changing the meaning of an existing Turn.

## ADR-075 - Core and the three host Capability Packs are one atomic slot

- Status: superseded by ADR-082.
- Decision: a v1 product ReleaseManifest that declares any host Capability
  Pack must declare the exact browser/image/sandbox archive+sidecar set for the
  selected platform and architecture. The InstallCoordinator downloads and
  independently resumes those six artifacts in signed source order, verifies
  both outer ReleaseArtifact and inner CapabilityPackManifest identities, then
  projects them into the Core payload before SlotStore performs its atomic
  candidate rename. `.slot.json` binds both the signed Core payload digest and
  the complete Core+Pack payload digest, but is not itself treated as a signing
  root: verification reconstructs the Core sub-tree against the signed Core
  archive and authenticates each Pack separately. Current pointers never
  identify a partially projected set. Activation, prior-slot validation and Bootstrap
  launch revalidate the same set through an injected product verifier; the
  update domain does not import Capability internals.
- Compatibility: a signed manifest with zero `capability-pack-*` artifacts is
  an explicit legacy Core-only branch for migration/tests. Once any Pack is
  declared, absence of another Pack, an unavailable content verifier or any
  path/content mismatch fails closed. The v1 Candidate recipe always contains
  all twelve target trees, so formal releases cannot use Core-only mode.
- Consequence: mirror failover, user confirmation, health rollback and known-
  good retention move Core and its execution dependencies together. A Pack
  failure cannot silently activate a new Core, and a failed composite staging
  verification deletes the unprotected candidate instead of making it reusable
  on restart.

## ADR-076 - Candidate identity includes a complete repository dependency lock

- Status: accepted.
- Decision: Python build/runtime dependencies are resolved into five
  repository-owned Python 3.11.9 profiles (`bootstrap`, `runtime`, `dev`,
  `cloud`, `platform-stage`). Every requirement is exact, binary-only at
  installation time and carries one or more PyPI SHA-256 hashes; installation
  uses `pip --isolated --require-hashes --only-binary --no-deps`. The local
  EcoreX source is installed only afterward with `--no-deps
  --no-build-isolation`. Node 22.23.1 uses the committed lockfile through
  `npm ci`. Workflows reject naked pip/npm installs, floating toolchains and
  non-40-character Action references.
- Identity: the canonical lock-manifest SHA-256 participates in Candidate
  `build_digest`/`release_id`, release metadata, stage receipts and CycloneDX
  SBOM. Platform staging compares the installed distribution inventory against
  the selected lock before emitting an attestation; an extra, missing or
  different package fails closed. The current reviewed lock identity is
  `2777443fb28ef39cc2a4fa7e4ba033899f3288624128709d032d7a42b0d2346d`.
- Consequence: CI and protected runners cannot silently resolve a newer
  transitive package while producing the same EcoreX release identity, and
  user machines never run pip/npm assembly. A lock or resolver upgrade is an
  explicit reviewed release input. macOS support begins at macOS 11 because
  the selected universal binary closure cannot honestly support the retired
  macOS 10.9 x64 wheel tag.

## ADR-077 - Public Bootstrap freshness is renewed online without release mutation

- Status: accepted.
- Decision: the stable Bootstrap pointer has two independent trust roles. The
  offline release key signs the immutable sequence, revision and release target;
  a distinct online KMS/HSM publication key signs only a domain-separated,
  maximum-24-hour freshness envelope bound to the authority digest. A Control
  Plane-owned refresher performs startup catch-up and hourly checks, renewing
  eight hours before expiry by default. Configuration is bounded and the
  signer executable/optional adapter are digest pinned and verified against the
  independent publication keyring.
- Durability: one database lease owns each attempt. Exact prepared bytes are
  append-only and reused after restart; staging, object CAS, HTTPS readback and
  canonical activation use the existing publication saga. A restart can also
  reconcile an activation whose exact public readback committed immediately
  before freshness-success bookkeeping stopped. Deterministic request IDs plus
  Control Plane idempotency make retries non-duplicating.
- Safety: renewal must preserve the same sequence, revision and target and does
  not create an update signal or rollout. Missing signer, signature failure,
  publication failure or readback failure is explicit in durable state, audit
  and outbox. The previous database-active pointer is retained; an active
  degraded or expired pointer blocks readiness. Operators have read-only status
  and one-shot same-authority refresh commands, while normal renewal requires
  no daily build/stage script.
- Role and scheduler hardening: release/publication separation is checked by
  SHA-256 fingerprint of the raw Ed25519 public key, not only by alias/key ID;
  the configured KMS signer public key is checked again. With automation
  enabled, readiness requires signer configuration, a live scheduler task, a
  bounded-age heartbeat and no scheduler error even before the first pointer
  exists. Unexpected loop failures enter the durable failure/outbox path and
  retry with a bounded delay rather than silently terminating.
- Manual mutation identity: an omitted operator request ID is never derived
  from mutable freshness time. The CLI persists one pending request before
  network mutation, binding it to endpoint, purpose and immutable authority
  digest. Ambiguous responses retain it, observed success clears it, and an
  authority change invalidates it with local audit evidence.

## ADR-078 - Candidate gates attest executed work and are release-bound

- Status: accepted; its stage-receipt cardinality is amended by ADR-082.
- Decision: a release gate is not inferred from a fixed list written at the end
  of a successful-looking quality job. Browser E2E is executed through
  `npm run test:e2e`; migration evidence executes the copy-on-write, Product
  coordinator, quarantine, released-schema and activation rollback contracts;
  image shared-storage and soak gates execute against isolated, digest-pinned
  PostgreSQL and MinIO services with exactly two node identities. The four-hour
  soak is a required protected-runner job. An unavailable protected runner
  leaves Candidate construction blocked; it never produces a skipped or
  synthetic passed receipt.
- Binding: browser, Windows platform-stage and image execution evidence are
  combined only after the signed Candidate exists. The derived evidence binds
  commit SHA, workflow run, `release_id`, `build_digest`, Candidate receipt and
  all eight Windows Core/Bootstrap/Pack stage receipts. Generic gate receipts
  hash that derived evidence.
- Source authority: Candidate checkout fetches and verifies the complete fixed
  v0.3.0 commit object set before the released-schema migration test. Both CI
  and Candidate scan the full current-v1 filesystem before generation, reject
  relevant untracked files and check whitespace on every source file. Lint and
  compilation cover Runtime, tests, every current-v1 release script,
  platform-staging and Capability Pack Python while excluding historical
  release scripts.

## ADR-079 - Progressive disclosure is the global Runtime invocation boundary

- Status: accepted.
- Decision: every model-callable Tool contributed by Core, a signed Capability
  Pack, MCP or another verified provider uses the same closed loop:
  `Catalog -> Availability -> Governance -> Ranking -> Search -> Exact Describe
  -> Durable Grant -> Invocation Recheck`. Only a bounded Core control surface
  is direct by default: capability search/describe, Skill search and safe
  workspace read. Most network, browser, vision, media, shell, connector and
  provider tools are deferred. Unavailable, unhealthy, stale or denied tools
  are hidden and cannot be elevated by search, model output or React.
- Authority: `tool_search` returns bounded summaries and never grants execution.
  A successful exact `tool_describe` execution is written before streaming and
  grants only the exact tool/version to the same durable Job, Thread, Turn,
  capability snapshot and permission snapshot. Caller booleans, Gateway
  `disclosed_tool_ids`, model-selected names and in-memory caches are hints only.
  Runtime reconstructs the grant from SQLite after restart and repeats current
  permission, extension revision, approval, schema, idempotency and sandbox
  checks immediately before dispatch.
- Layering: Skills use a separate progressive instruction loader because they
  are declarative guidance rather than execution authority; their eventual
  Tool/MCP/Connector calls still cross this Tool boundary. Connector menus and
  Capability Packs are catalog/lifecycle projections, not alternate dispatch
  paths. Internal lifecycle operations that are never model-callable stay out
  of the catalog entirely.
- Consequence: large installations do not send every schema on every model
  request, image intent cannot erase sibling capabilities, an invented deferred
  call cannot create a misleading approval card, and Replay can explain the
  complete candidate/ranking/disclosure/denial chain. The cost is one explicit
  discovery round for deferred tools; exact user selection and durable reuse
  within the same Turn keep that cost bounded without weakening authority.

## ADR-080 - A schema-v2 image share is all-or-nothing

- Status: accepted.
- Decision: every primary or secondary `family=image` Artifact included in a
  new schema-v2 ShareSnapshot must reference one immutable `preview` or
  `thumbnail` rendition in PNG, JPEG, WebP, GIF or AVIF form. One rendition is
  limited to 16 MiB and one snapshot to 64 MiB. The local snapshot builder,
  durable worker, HTTPS publisher, Control Plane publication transaction and
  public renderer all apply the same contract. Image intent or a valid primary
  source blob cannot substitute for a rendition.
- Failure contract: missing, oversized, unsupported, invalid and aggregate-
  oversized previews return stable path-free codes with a user action and
  explicit retry meaning. A frozen invalid Durable Job is terminal because
  retransmitting identical bytes cannot repair it; the user may recreate the
  share after the Artifact pipeline supplies a valid rendition.
- Compatibility: schema v1 canonical bytes and stored public snapshots remain
  readable, but schema v1 is no longer accepted for new publication. The
  Control Plane never decodes or resizes an original Artifact. This prevents a
  successful link from silently rendering an image as a generic file row.

## ADR-081 - Recovery mutations use a separate, closed authority lane

- Status: accepted.
- Decision: a Critical Runtime cannot grant business mutation by allowlist.
  Managed-session logout and exact local update activation instead use a
  process-local gate whose complete scope set is fixed in code. Update checks,
  downloads, rollout changes and every Thread/Turn/Artifact/Connector mutation
  remain under the ordinary Runtime gate.
- Update authority: recovery activation accepts only the transaction named by
  both Runtime update state and the append-only installer journal while both
  are `awaiting_user`. The locally stored release manifest, artifact and staged
  slot are cryptographically re-verified. No cloud lease or feed request is
  made after logout; the loopback Runtime bearer, exact Origin and CSRF token
  form the host installer credential.
- Concurrency: recovery permits are captured before async dispatch, asserted
  after await and propagated to SQLite pre-commit through request ContextVars.
  Gate locks never span awaits. A closed recovery permit cannot publish a late
  database result; an interrupted filesystem activation remains in the
  installer's recoverable journal rather than mutating business Runtime data.
- Consequence: account revocation and a previously verified security update
  remain possible during read-only protection without turning Critical mode
  into a general mutation bypass or an unauthenticated control-plane client.

## ADR-082 - A formal Candidate contains six required Capability Packs per host

- Status: accepted; supersedes the topology clauses in ADR-072 and ADR-075 and
  amends ADR-078.
- Decision: each of Windows x64, macOS arm64 and macOS x64 contributes one Core,
  one Bootstrap and exactly six Capability Pack stage trees: `browser`,
  `channels`, `image`, `ocr`, `office` and `sandbox`. Candidate assembly accepts
  exactly 24 receipts, eight per target. One host slot contains the Core plus
  all six Pack archive/sidecar pairs; Bootstrap and Runtime both reject a
  missing or unexpected host Pack.
- Contract boundary: browser/image/sandbox bind backend-owned model-callable
  `ToolSpec` digests. Channels, OCR and Office are service-only Packs bound to
  `channels.adapters`, `ocr.extract` and `office.formats`; they do not create
  fake tools. OCR must recognize a staged fixture from Pack-local dependencies.
  Office proves DOCX/XLSX/PPTX/PDF create/read/validate only and does not claim
  high-fidelity rendering. A future renderer must have its own signed provider
  contract and real rendition-quality evidence.
- Consequence: dependency installation, capability availability, release
  signing and update activation share one deterministic Pack catalog. A schema,
  script or installer that still assumes three Packs or 15 stage trees fails
  before Candidate signing.

## ADR-083 - Update drain follows the durable slot pointer across interruption

- Status: accepted.
- Decision: Runtime closes new business admission before migration and pointer
  activation while allowing only already-leased durable work to checkpoint or
  finish. If the coordinator returns or is interrupted after the candidate
  becomes the current slot, it derives that boundary from the durable active
  transaction, journal, prior pointers and current pointer. Admission stays
  closed even when response persistence or restart scheduling did not finish;
  unreadable boundary state also fails closed and Bootstrap recovery converges
  the exact transaction.
- Consequence: a process interruption immediately after atomic pointer switch
  cannot reopen the old Runtime for new work. A pre-boundary drain timeout
  remains reversible, keeps the signed candidate staged and returns to
  `awaiting_user`.

## ADR-084 - Rollback is a signed release operation and downloads share a CAS

- Status: accepted.
- Decision: the administrator can create, activate, pause or terminate a
  rollback only to an older published release with prior known-good rollout
  evidence and a compatible target matrix. Control Plane issues a short-lived,
  nonce-bound Ed25519 authorization over source/target release, build, Core
  artifact, client and host identity. Runtime verifies it under a trust role
  distinct from the offline release key and consumes it exactly once before
  using the ordinary user-confirmed drain/activate/health chain.
- Download boundary: full Core, delta and Capability Pack bytes enter one
  content-addressed cache only after signature, size and SHA-256 verification.
  Per-digest cross-process single-flight, corruption quarantine and bounded
  age/capacity GC prevent duplicate downloads without allowing one transaction
  to trust another transaction's unverified temporary file.
- Consequence: routine rollback is operable from the product console without
  weakening downgrade protection, and concurrent installs reuse immutable
  verified bytes instead of racing separate scripts.

## ADR-085 - Project identity and Composer facts remain Runtime-owned

- Status: accepted.
- Decision: a project is a Runtime record backed by a canonical directory, not
  a browser preference. New-Thread metadata carries only the selected project
  identity; Runtime validates it and publishes the canonical name/path. The
  first-message durable outbox freezes that metadata with the message intent.
- UI consequence: v0.3 project/general conversation choices, model selectors,
  permission label and usage meters are view projections. Unknown quota or
  context dimensions display `—`; character counts and local estimates cannot
  be presented as provider usage.
- Performance consequence: the sidebar may be a same-origin content-addressed
  deferred module with a geometry-preserving fallback. The initial-JavaScript
  budget is not raised to pay for project navigation.

## ADR-086 - Composer attachments and usage are evidence-backed Runtime projections

- Status: accepted.
- Attachment decision: a selected user file becomes an account-scoped internal
  source Artifact with a unique opaque attachment/revision identity. The WebUI
  may retain only that identity and non-sensitive display metadata in its
  bounded durable message outbox; bytes, paths, credentials and raw model
  responses are excluded. Runtime resolves identities before Turn acceptance,
  persists the exact bound list in immutable Turn metadata and fences reads to
  the same execution scope. The attachment reader is normally deferred and is
  promoted by a distinct Runtime-context planner fact only for such a bound
  Turn.
- Usage decision: the Composer never derives tokens from browser text,
  character count or an optimistic request. Day/week values aggregate only
  provider `model.response_completed` usage facts, while context is the most
  recent provider-reported input size plus the selected model's signed
  compaction threshold. Managed quota remains a separate signed-session fact.
- Consequence: a refresh, retry, reconnection or model change cannot silently
  manufacture an attachment authority or a quota/usage value. The visual
  meter can be unavailable, but it cannot lie.

## ADR-087 - Composer placement follows conversation state, not implicit Grid order

- Status: accepted.
- Decision: the only centered Composer is the one presented while the user is
  choosing a new general or project conversation. It is rendered as part of
  that chooser and uses the same Runtime-backed input contract. An established
  Thread renders the Composer in a dedicated bottom Workspace region.
- Layout boundary: Workspace children own explicit Header, status, Timeline
  and bottom grid rows. An empty status stack may be absent from paint, but it
  cannot cause later children to auto-place into a different semantic row.
  Mobile active-Turn controls have bounded columns with a persistent accessible
  send name rather than relying on an offscreen text button.
- Compact-navigation boundary: when the 88px rail visually hides a label,
  every action retains an explicit accessible name. Project-session creation
  uses the same icon-plus-label pattern as other compact actions; it must not
  leave direct low-contrast text as the only compact affordance.
- Consequence: conversation mode, viewport size and a transient empty banner
  cannot make the Composer appear mid-workspace or make a queue action
  unreachable. The behavior is protected by exact viewport, axe and browser
  interaction regressions.

## ADR-088 - Runtime handler binding reconciles stale absence facts only

- Status: accepted.
- Decision: `RuntimeAvailability` represents executable handler availability,
  while a Turn's immutable attachment list represents resource scope. When
  RuntimeComposition binds the non-replaceable `input_attachment_read` Core
  handler, it clears only its own low-level
  `verified_handler_not_installed`/`input_attachment_runtime_not_bound` facts.
  It must preserve administrator hard-deny, network, sandbox, Pack and any
  other independent denial. If no Artifact-backed reader exists, it emits the
  explicit `input_attachment_runtime_not_bound` fact.
- Consequence: progressive disclosure can keep the reader deferred on ordinary
  Turns and promote it only for backend-bound uploads, without producing the
  contradictory state of a direct tool with a verified handler that the same
  availability snapshot says is absent. The same reconciliation pattern is
  available for future trusted Core handlers.

## ADR-089 - Provisional activation does not duplicate Pack verification

- Status: accepted.
- Decision: Bootstrap verifies the exact signed Capability Pack set immediately
  before it starts a provisional Runtime. The nonce-bound probe then validates
  the selected signed slot, sandbox attestation and verified Web bundle, but it
  does not re-hash every Pack, construct Pack adapters or open the credential
  vault while it exposes no business endpoints. Once Bootstrap confirms the
  proof, it starts a full Runtime that still verifies/binds every Pack before
  it crosses the data barrier or accepts traffic.
- Safety boundary: a missing/invalid proof still stops the candidate within the
  ordinary bounded loopback window. The Bootstrap's Pack verification and the
  full Runtime's independent verification are retained; a Pack failure before
  data writes follows the existing pre-data rollback path.
- Consequence: first install and update health cannot falsely fail merely
  because the same large immutable Browser/OCR Pack bytes were read twice in
  serial. The activation probe remains fast, no-traffic and cryptographically
  bound rather than becoming a weaker long-wait workaround.

## ADR-090 - Runtime startup diagnostics are fixed, nonce-bound and advisory

- Status: accepted.
- Decision: Bootstrap issues one opaque token for each Runtime child and
  permits that child to write only a tiny exact-schema record containing the
  same token and a fixed safe startup-stage identifier. The record lives under
  a fixed install-root directory, is consumed and deleted after child exit,
  and may never contain raw stderr, exception text, provider responses, local
  paths, credentials or command arguments.
- Safety boundary: Bootstrap treats a missing, malformed, stale or forged
  record as unavailable. It never trusts the record for selection, health,
  confirmation, rollback or process control; signed slot and Pack validation
  remain the sole activation authorities.
- Consequence: a bounded Runtime configuration exit can distinguish a
  credential-vault, immutable Pack binding or later composition stage without
  reopening a secret-bearing stderr channel. Candidate drills and system
  observability gain actionable evidence while the release trust boundary is
  unchanged.

## ADR-091 - Process-Pack descriptors are generated and Pack Python is verified once per startup

- Status: accepted.
- Descriptor decision: repository `ecorex-pack.json` files are semantic source
  templates, not signed wire artifacts. Platform staging validates every field
  against the authoritative Pack/tool catalog, then emits exact sorted compact
  UTF-8 JSON with no trailing byte. Browser and Sandbox gates read those exact
  bytes before signing. Runtime retains its strict canonical-byte comparison;
  formatting drift is fixed at generation rather than accepted at execution.
- Interpreter decision: Browser and Sandbox share one signed relocatable
  Python closure. A full Runtime verifies that complete closure once for one
  synchronous composition and reuses only the resulting immutable identity
  while binding the remaining Pack set. A new composition, restart or process
  creates a new resolver and scans again; no cross-process or persisted trust
  cache exists.
- Build consequence: the platform stager keeps one independent post-write
  closure verification, but reuses that result for its remaining synchronous
  gates instead of performing a third identical scan. This removes redundant
  cold-start/build work without weakening activation-time or restart-time
  verification.

## ADR-092 - Product startup stages follow ownership boundaries

- Status: accepted.
- Decision: the packaged entrypoint distinguishes signed Runtime composition,
  ASGI application composition and loopback HTTP-server configuration with the
  fixed safe stage identifiers `runtime_composition`,
  `application_composition` and `http_server_configuration`. A more precise
  stage already produced by the Runtime loader is never collapsed into these
  aggregate stages, and trust/integrity failures keep their existing handling.
- Resource boundary: a completed dependency composition remains owned by the
  synchronous entrypoint until application construction and Uvicorn
  configuration both succeed. Either failure invokes idempotent unstarted
  cleanup; only then may ownership transfer to the FastAPI lifespan.
- Observability boundary: the stage code is advisory and contains no exception
  text, path, credential or provider response. It can locate a failed startup
  layer but cannot affect signed-slot selection, activation, rollback or
  readiness.
- Consequence: Candidate failures become actionable without reopening stderr,
  and an HTTP configuration rejection cannot leak managed transports between
  Bootstrap launches.

## ADR-093 - Signed Core is the timezone-data authority

- Status: accepted.
- Decision: Core ships an exact hash-locked `tzdata` distribution. The
  isolated platform probe and product server explicitly call
  `zoneinfo.reset_tzpath(())` and clear its cache; Bootstrap additionally sets
  `PYTHONTZPATH=""` for non-isolated paths. Python `zoneinfo` must therefore
  resolve named IANA zones from the signed product closure on every supported
  host, independent of a system database or developer environment.
- Validation boundary: the generated Core interpreter imports the bundled
  distribution and resolves `Asia/Shanghai` before platform staging may emit
  a Core receipt. Source-interpreter success is insufficient evidence for a
  packaged Runtime. Dependency identity remains part of the Candidate lock
  manifest and SBOM. Supply-chain preflight must license exactly every
  canonical package/version in the Runtime lock; a successful subset is not a
  valid receipt.
- Rejected alternatives: falling back to UTC, the current numeric offset or a
  host-specific timezone registry would make day/week projections silently
  wrong and would produce different product behavior across Windows and
  macOS. Runtime therefore fails closed if its signed named-zone authority is
  absent or invalid.
- Consequence: usage windows, scheduler deadlines and audit timestamps share
  one deterministic timezone authority across install, update and rollback;
  host drift cannot change application composition after a signed build.

## ADR-094 - Public ASGI route dependencies belong to signed Core

- Status: accepted.
- Decision: every Python distribution imported while constructing the public
  FastAPI application or registering one of its routes is a direct Runtime
  dependency, part of the hash-locked Core closure and license inventory. A
  developer extra or an already installed host package cannot satisfy the
  product contract. Core's isolated probe imports the compatibility symbol
  FastAPI needs for multipart route registration before a stage receipt is
  emitted.
- Version boundary: promote the already reviewed
  `python-multipart==0.0.26` baseline without opportunistically upgrading it.
  Runtime, Cloud and platform-stage profiles inherit the same exact hashes;
  dev inherits it from Runtime rather than declaring a second root.
- Diagnostic boundary: a `RuntimeError` raised synchronously during FastAPI
  construction is classified as the fixed redacted
  `application_composition` stage and closes the unstarted composition once.
  Trust/integrity failures retain their own handling, and no native exception
  text, path or form value is persisted by Bootstrap.
- Rejected alternatives: relying on a source-interpreter import, lazily
  installing the package on a user machine, or removing upload routes from
  the probe would reproduce environment-dependent startup. Mutating a failed
  signed slot for proof would invalidate its identity and is not acceptable
  Candidate evidence.
- Consequence: first install and update cannot pass Core staging with a
  Runtime that later fails merely by registering its supported attachment or
  Artifact upload endpoints.

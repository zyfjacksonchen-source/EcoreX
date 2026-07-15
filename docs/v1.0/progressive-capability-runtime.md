# EcoreX v1.0 progressive capability Runtime

> Status: accepted product contract. The Tool Search -> exact Describe ->
> batch-scoped durable Grant and current-authority Admission closures are
> implemented. The model-visible count/schema budget is also enforced by both
> Runtime and Gateway. The Skill Search -> exact revision Read resource closure
> is implemented and covered by executable restart/forgery boundary tests.

## Decision

Progressive disclosure is the default invocation architecture for every
capability the model can call. It is not applied to Runtime internals, plugin
containers, release services, ordinary UI commands or backend-only lifecycle
operations.

The authoritative closure is:

```text
Catalog
  -> current Availability
  -> frozen Governance
  -> intent Ranking
  -> bounded Search
  -> exact Describe / Skill Read
  -> durable Grant
  -> current-authority Admission
  -> dispatch
  -> durable Outcome
```

Ranking is evidence, never authority. It can make an eligible capability easier
to discover but cannot install it, make it available, bypass a deny, remove an
approval or execute it.

## Product boundary

| Capability class | Default exposure |
| --- | --- |
| `tool_search`, `tool_describe` | direct Core control surface |
| bounded workspace `read`, Skill search | direct |
| `fetch`, `vision`, `imagegen`, browser/CDP, shell | deferred |
| Skill content, MCP tools, Connector actions, external writes/share/send | deferred |
| Runtime/Pack/provider/plugin containers and UI commands | not model-callable |
| missing, offline, unhealthy, quarantined, revoked, denied or incompatible | hidden |

Third-party metadata cannot declare a trusted routing facet or direct exposure.
MCP tools always enter the catalog as deferred. A reviewed administrator policy
may add a separate explicit allowlist in a future contract; provider metadata
alone can never do so.

### Provider provenance and fair search

Every Tool contract, capability decision, search summary and exact Describe
projection carries one backend-created provider record:

```text
kind, provider_id, revision_id, trust
key_id?, evidence_sha256, product_reviewed
```

Core defaults are fixed to the reviewed `ecorex.core@core-contract-v1`
identity. The `mcp.*` namespace cannot use that default. An MCP Tool must use
the exact `mcp.<extension_id>:` namespace and a non-secret provenance record
derived from a currently verified Extension manifest: exact `extrev_*`
revision, trust verdict, signing-key identifier and evidence digest. Detached
signature bytes and manifest bodies never enter model context or traces.

MCP trust is an audit fact, not product review. Even a Core-bundled MCP
transport remains deferred, has no routing facets or priority bias, and has
`product_reviewed=false`. Only product-owned Core Tool contracts receive the
reviewed search reserve. Consequently a provider cannot self-assert builtin
trust, use its description/tags to become direct, or acquire reviewed image
routing.

The provider-selection policy is included in the discovery-policy digest.
Exact references are selected before fairness, so an exact query with
`limit=1` remains exact. Broader bounded search reserves at most half its
remaining slots for matching reviewed Core contracts, then repeatedly selects
the least represented exact `(kind, provider_id, revision_id)` group. Ordering
inside each choice remains deterministic by match class, specificity, frozen
score, trust verdict and provider identity. This prevents one 256-tool MCP
catalog from monopolizing results while preserving Core visibility and stable
restart/replay bytes; authority is never guessed from `tool_id` text.

## Durable identities

Search returns summary records with an immutable discovery identity:

```text
tool:<tool_id>@<tool_version>
skill:<extension_id>@<revision_id>
```

Tool Search does not return invocation authority: an exact Describe is still
required. Skill Search is different only at the resource layer: its completed,
recomputed result may expose the generic `skill_read` endpoint, but grants that
endpoint access to only the exact Skill revision IDs present in that search
result. Exact describe/read results are written before they are streamed to the
model. The resulting grant is scoped to:

```text
job_id, thread_id, turn_id, execution_batch_id
capability_snapshot_id, permission_snapshot_id, extension_snapshot_id
tool_or_skill_id, version_or_revision
search_execution_id, describe_or_read_execution_id
```

Caller booleans, model-selected names, Gateway fields and process memory are
not grants. Runtime reconstructs grants from SQLite after restart.

The model-facing Tool path is stricter than Runtime's internal catalog API.
`tool_describe` accepts only the exact `tool:<tool_id>@<tool_version>` value
emitted by a completed `tool_search` in the same Job, Thread, Turn, execution
batch, capability snapshot and permission snapshot. Runtime recomputes the
recorded search before describing, then persists the selected search execution
ID and result digest in the Describe outcome. Bare names, aliases, guessed or
stale IDs, forged result projections and cross-batch facts never mint a grant.
Backend approval/UI code may still call the canonical internal describe API;
that API is not the model handler and does not create disclosure authority.

The model-facing Skill path accepts only
`skill:<extension_id>@<revision_id>` plus optional frozen `reference_ids`.
`skill_search` returns a schema version, the batch-frozen Extension snapshot
ID, the immutable contribution-snapshot ID and bounded display metadata; it
never returns a host path, CAS digest or source filename. Before reading,
Runtime finds a completed `skill_search` in the same Job/Thread/Turn/execution
batch and capability/permission snapshots, recomputes the complete result from
the frozen Extension inventory, and verifies the canonical result SHA-256. The
read outcome binds the search ToolExecution ID and that digest. Bare names,
aliases, another Skill's reference ID, old revisions, forged results and facts
from another batch fail closed. Both generic endpoint disclosure and exact
resource authority are reconstructed from SQLite after restart.

Immediately before a side effect, Runtime writes an invocation admission that
binds the exact arguments digest, current permission authority, approval fact,
effective sandbox, provider revision and current availability. Admission is the
linearization point against permission revocation. A newer setting may tighten
an old Turn but cannot broaden the frozen authority it accepted with.

## Image generation and editing

Image language maps to reviewed semantic facets such as
`media.image.create` and `media.image.edit`. It raises the best eligible
implementation's discovery rank. It does not set provider `tool_choice`, name a
concrete implementation in the generic planner or delete read, vision, browser,
fetch or shell.

The current built-in implementation is `imagegen`. A reviewed replacement that
implements the same effect/facet contract can win without changing routing
code. Explicit structured selection may expose an exact eligible tool directly;
ordinary semantic intent remains ranked and deferred.

## Skill, MCP and Connector layering

- A Skill is declarative guidance. Search/read is revision-scoped and Skill
  content never grants a Tool, MCP or Connector permission. Explicitly naming
  a Skill only ranks its search result; it never directly promotes
  `skill_read` or authorizes content.
- MCP tools enter the same Tool catalog with provider/revision provenance,
  bounded schema and metadata, deferred exposure and current provider fencing.
- Connector definitions and instances own login, health and actions. A
  model-callable Connector action is projected as a deferred governed tool;
  the Connector menu itself is a backend projection, not a tool.
- Capability Packs install executable handlers but do not bypass catalog,
  disclosure, permission, sandbox or admission checks.

Connector execution uses its own exact specialization of the same boundary:

```text
connector_search -> connector_describe -> connector_read | connector_write
```

- Search results bind one instance, account, action and frozen Connector
  catalog contract digest. Describe grants only that opaque discovery ID to
  the same execution batch; a guessed ID or a grant copied across a Turn is
  rejected before provider dispatch.
- Default-mode writes create an informed approval naming the Connector,
  account and exact action. The durable approval checkpoint contains only
  public descriptor metadata and its digest. Runtime reconstructs the same
  Search/Describe fact after the human response and rejects a changed account,
  action or contract before admission.
- Missing authorization and insufficient scopes become a durable generation
  of the Connector-login interaction. OAuth flow ownership, callback consume,
  instance activation or credential swap, interaction completion and authority
  refresh are transactionally fenced and resume after restart.
- All model-originated reads and writes use a stable local idempotency key.
  Provider writes retain that same key at the provider boundary. Concurrent
  callers wait for the first local publication and never dispatch twice.
- Every successful provider result first crosses `connector_result_staging`.
  Canonical JSON up to 512 KiB is bounded in SQLite; larger JSON is stored in
  Artifact CAS and the stage retains only digest and authority metadata. One
  Runtime transaction then commits the exact replay envelope, Connector
  invocation/idempotency/outbox, and—when Artifact-delivered—the user-thread
  completed Artifact Item and event. Restart and late-success recovery finalize
  locally without contacting the provider.
- Rejected schema/secret results become a bounded `result_unavailable` success
  receipt. The receipt hashes only its non-secret receipt identity, never the
  rejected bytes, and exact replay cannot become a credential-guessing oracle.
  Oversized data is exposed as a secondary `data_export` JSON Artifact and can
  be read only through the protected, thread-scoped `artifact_read` Core tool.
- A timed-out validated success commits through the same staging path; an
  unresolved outcome remains explicitly reconcilable. Once a stage exists,
  human reconciliation cannot reopen the provider replay fence.
  Disconnect uses a stable provider idempotency key plus a durable revocation
  claim, and maintenance resumes abandoned draining/revoking generations.

## Context and concurrency budgets

The closed loop must remain bounded under many installed providers:

- one sealed Runtime catalog contains at most 1,024 tools;
- one MCP provider contributes at most 256 tools and cannot monopolize search;
- one model round can see at most 16 complete Tool descriptors;
- at most 12 of those descriptors may come from durable deferred grants;
- one canonical descriptor is at most 96 KiB and the canonical descriptor
  batch is at most 256 KiB;
- direct Core/planner tools are projected first in frozen score order and are
  never truncated to make room for a third-party grant;
- search returns bounded summaries, not complete schemas;
- over-budget deferred grants remain searchable deferred IDs, but their schema
  and invocation authority are suppressed for that model round;
- an oversized frozen direct set produces a typed fail-closed Turn outcome;
- `ModelGatewayRequest` validates the limits and the fixed provider adapter
  independently repeats them before constructing an upstream request;
- large outputs become Artifact handles, pages or summaries;
- server-side Responses compaction is enabled by the frozen managed-model
  policy for long conversations.

Exceeding a budget produces a typed, observable failure or narrower result. It
must not crash Runtime, silently hide an unrelated Core capability or fall back
to an ungoverned dispatch path.

## Retry and crash semantics

- Search, describe, Skill read and other read-only operations may retry.
- An idempotent write requires one stable idempotency key across attempts.
- A non-idempotent call is never automatically retransmitted after dispatch
  uncertainty.
- MCP initialize and catalog negotiation may retry before `tools/call` because
  no business side effect has crossed the boundary.
- A crash before durable admission is safe to resume. A crash after admission
  but before a verifiable result becomes `uncertain` and requires explicit
  human resolution.

## Required observability

Every call must be traceable as:

```text
search_id -> describe_id -> grant_id -> admission_id -> tool_execution_id
```

The trace includes execution batch, candidate scores, suppression reasons,
provider/revision, canonical `tool_schema_bytes`, projected/suppressed IDs,
`tool_projection_budget_version`, frozen and current policy IDs, approval wait,
sandbox, retry/uncertainty reason and final outcome. Descriptor schemas are not
copied into observability events. Replay reads these facts; it never reruns a
newer discovery policy to explain an old Turn.

## Release blockers

- Guessed, alias-only, cross-Job, cross-Turn, cross-batch and stale-version
  deferred calls fail before Tool Item or approval UI creation.
- Permission revoke/admission races have one deterministic order; default to
  full access never broadens an already accepted Turn.
- Selecting Skill A cannot read Skill B or inherit Tool permissions (enforced
  by executable cross-Skill, cross-reference and restart tests).
- Provider floods and maximum-size schemas cannot exceed request/catalog
  budgets or starve reviewed Core results.
- Image intent keeps sibling tools discoverable and never hard-selects
  `imagegen`.
- Restart reconstructs the same grants, batches, approvals and uncertain
  outcomes without duplicate side effects.

## Durable dispatch authority

The final `Current Authority Admission` stage is an append-only permit, not an
`approved=true` request field. Runtime persists it in the same SQLite authority
as the ToolExecution before calling a handler. The permit includes the exact
execution batch and verified current permission-ledger chain head. Its database
transaction rechecks that chain head, which closes both same-process and
separately locked Runtime permission races.

The state distinction is deliberate:

```text
started + no admission     -> no side effect crossed; safe to resume
started + admission        -> dispatch may have crossed
non-idempotent + admission -> crash/unknown result requires human resolution
```

Current availability and current permission may only remove authority from the
frozen Turn. The final check is repeated even when model projection and initial
approval-card projection already succeeded.

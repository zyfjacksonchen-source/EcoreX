# EcoreX v1.0 image intent routing

## Product contract

An image-generation or image-editing request should make the best eligible
image capability easy for the model to choose. It must not force a concrete
tool, hide unrelated capabilities or let the React client decide execution.

The route therefore targets a desired effect and reviewed semantic facet:

```text
user intent
  -> media.image.create | media.image.edit
  -> generate_media effect
  -> eligible reviewed catalog implementations
  -> ranked model-visible tools
  -> model tool choice
  -> runtime invocation validation
```

`imagegen` is the current built-in implementation. It is not named by the
generic planner, and another reviewed implementation with the same contract can
replace it without changing planner code.

## Decision pipeline

1. **Catalog** loads immutable `ToolSpec` records. Effects, routing facets,
   schemas, idempotency, sandbox, approval and required Packs are backend facts.
2. **Availability** checks the installed signed Pack, managed model modality,
   provider/runtime health, platform and Connector dependencies.
3. **Governance** applies the frozen default/full/admin-deny permission
   snapshot. An unavailable or denied capability is hidden and cannot receive a
   routing promotion.
4. **Intent evidence** evaluates a bounded, versioned product policy. Strong
   create/edit requests rank a matching eligible facet first in deferred
   discovery; semantic intent never grants direct invocation authority.
   Negation, diagnostics, pricing, architecture and feature-discussion
   requests suppress that ranking. Local prefix/suffix context distinguishes
   `生成图片说明` from `生成图片并写图片说明`; bounded ordered clauses let the
   latest explicit correction win. Multiple matching rules contribute only
   their maximum boost, so repeated text cannot stack priority.
5. **Explicit choice** of an eligible exact tool alias is stronger than any
   semantic hint and may promote that exact tool to direct exposure. Unknown
   names remain in the immutable trace and fail closed.
6. **Exposure** sends only the small direct Core set to the managed chat model
   and advertises deferred tool IDs through `tool_search`. Exact
   `tool_describe` writes the snapshot-bound disclosure grant before the full
   schema is sent. Image intent never removes read, fetch, vision, CDP/browser
   or shell.
7. **Invocation** re-resolves the tool against the frozen capability and
   permission snapshots, validates its schema, approval and idempotency key,
   then submits image work to the durable image orchestrator. Ranking is never
   execution authority.

This keeps the useful Codex separation between capability gating,
model-visible tools, deferred discovery and runtime dispatch. Clean-room source
references are pinned in `decision-log.md` ADR-009.

## Trust and replay properties

- Free-form Skill/MCP descriptions and intent tags cannot claim a trusted
  routing facet or arbitrary score. Core names and aliases are reserved.
- The routing policy ID, SemVer and digest, every candidate score, matched
  evidence, suppression reason and final exposure are stored in the Turn's
  immutable capability snapshot.
- Unicode is normalized with NFKC; zero-width format characters cannot bypass a
  negative phrase. Invalid Unicode fails closed and routing work is capped at
  64 KiB with bounded evidence.
- Equal candidates have deterministic tool-ID tie breaking independent of
  registration order. Replay reads the recorded snapshot rather than rerunning
  a newer policy.
- Missing Pack, offline provider and administrator hard deny always beat intent
  evidence. The client cannot submit an authoritative enabled-tool list.

## Expected behavior

| Request | Result |
| --- | --- |
| `请读取说明并生成一张海报` | reviewed image implementation first in deferred discovery; read remains direct |
| `用参考图改图，必要时打开网页` | image edit ranks first; vision/browser remain discoverable |
| `Use shell to generate an image` | explicit eligible shell direct; image capability remains deferred and ranked |
| `生图失败，只分析原因` | image capability remains deferred; no automatic invocation |
| `生图失败；请重新生成一张图片` | later explicit retry ranks image generation first |
| `生成一张图片，但只分析方案` | final analysis-only clause cancels image routing |
| `生成图片说明` | text/meta context; no generation promotion |
| `image2 有什么价格？` | model/catalog question; no generation promotion |
| image Pack missing or admin denied | image capability hidden with recorded reason |
| reviewed replacement implements the same facet/effect | replacement is routed without planner changes |

## Release gates

- Planner source contains no concrete image tool or `media.image.*` identity.
- Strong Chinese/English create and edit requests rank an eligible image
  implementation first without deleting another eligible tool.
- Explicit eligible tools outrank the maximum semantic boost.
- Diagnostic, negated, document-context and feature-discussion requests do not
  rank image generation.
- Untrusted MCP metadata cannot self-promote or collide with a Core alias.
- Worker-to-Gateway integration preserves deferred ranking, exact disclosure
  and the small direct Core surface.
- Invocation remains fail-closed for availability, policy, schema, approval and
  idempotency changes.

Focused implementation evidence is recorded in `implementation-log.md`; these
checks are rerun as part of the immutable candidate suite.

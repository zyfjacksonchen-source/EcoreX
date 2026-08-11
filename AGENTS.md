# e-Mate development rules

## CowAgent 2.1.5 is the Agent data-plane baseline

e-Mate keeps its desktop UI and enterprise control plane. The control plane may
manage only accounts/passwords, Token usage, version publication, and audit.
The Agent data plane must remain behaviorally compatible with upstream
CowAgent 2.1.5 at commit `e3ac1b952500f60934862c6bf0bd0de91b415ed8`.

- Do not add permission profiles, default/full-access modes, per-tool approval,
  administrator tool denies, keyword hiding, secondary schemas, or per-call
  release/lifetime gates that CowAgent does not have.
- A first-party tool has one model schema and one execution contract. Packs and
  adapters transport that contract; they may not reinterpret it.
- Preserve Cow lifetimes and out-of-box behavior for files, terminal, browser,
  Web Search/Fetch, image generation, Skills, MCP, channels, scheduled tasks,
  memory, and knowledge.
- Keep the current e-Mate channel display names/icons. Presentation must not
  select a different transport or execution policy.
- Local Skills and MCP configuration are user data. Enterprise metadata may
  describe them, but may not hide or disable a locally valid capability.
- Memory authority is `MEMORY.md` plus `memory/**/*.md`; knowledge authority is
  `knowledge/**/*.md`. The model and the e-Mate pages must read the same files.
  Restore Cow's `memory_search`/`memory_get`, prompt recall, proactive memory
  writes, knowledge index maintenance, and natural use of recalled facts.
- Account isolation, credential secrecy, Token accounting, audit collection,
  and immutable release verification remain observation/trust boundaries. They
  must not change ordinary tool availability, schemas, lifetime, or results.
- A deviation from Cow requires a reproduced OS/provider/legal/data-loss
  constraint and the smallest regression proving it. Do not invent a stricter
  e-Mate policy in anticipation of a hypothetical risk.

Use the development loop for this work: reproduce at the cheapest layer, add
one failing regression, fix the shared root, run that regression and one
affected subsystem check, then stop. Packaging, signing, deployment, and the
Release Train run only for an explicitly frozen release.

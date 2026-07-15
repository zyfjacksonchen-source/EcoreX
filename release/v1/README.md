# EcoreX v1 Candidate contracts

This directory documents the two public hand-off documents consumed by the
protected Candidate workflow:

- `stage-receipt.schema.json` binds one real platform Runtime or Capability
  Pack tree to its source commit, trusted workflow run, target, byte inventory
  and four platform receipts, including a per-tree supply-chain scan. It also
  records the protected stager executable/adapter digests and workflow run
  attempt.
- `candidate-recipe.schema.json` selects exactly 24 such trees: three Runtime,
  three Bootstrap and all six required Capability Packs for every target.
- `candidate-build-receipt.schema.json` describes the successful, externally
  Ed25519-signed Candidate receipt that binds those 24 receipts, staging
  provenance, Web tree and complete signed manifest artifact projection.

The executable validator in `ecorex.release.candidate` is intentionally stricter
than JSON Schema. It rejects links/reparse points, path collisions, changed
files, missing launchers/pack contracts, duplicate targets, incomplete gate
sets and receipts from a different commit or workflow. Schema validation is a
developer aid, not the signing trust boundary.

Recipe source URLs are roots, not mutable final aliases. Mirror/CDN roots are
channel-qualified and GitHub ends at `/releases/download`. ReleaseBuilder binds
those roots into `build_digest`, then appends `release_id` (replicas) or the
stable/unique-canary tag (GitHub) before signing the manifest.

No file in this directory is a Runtime or Pack payload. Real trees are emitted
only by the protected platform workflow through the repository-owned,
source-digest-pinned `platform-staging/stager.py`. The repository also owns the
Windows AppContainer/Job helper, macOS/Windows launchers, bounded Pack sources
and behavioral probes. Protected runners must still provide the target native
toolchain, locked Python profile, digest-bound production Runtime config and
real Playwright Chromium installation. If any dependency, native build or
behavioral probe is unavailable, the stage emits a typed failure receipt and
the Candidate does not sign or publish anything. Core and all six required
Packs are downloaded and activated as one verified slot; see
`docs/v1.0/capability-pack-platform-staging.md`.

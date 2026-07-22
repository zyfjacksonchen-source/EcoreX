# e-Mate current release closure ledger

Last updated: 2026-07-23 (Asia/Shanghai)

This file is the single recovery ledger for the active long-running release
goal.  It records only evidence that has actually been observed.  A build,
test, deployment, or user flow is not marked complete from intent or from a
narrower proxy.

## Completion contract

- Remove the Runtime read-only blocker without weakening invariant checks.
- Complete the authenticated message and continuous reasoning flow.
- Complete upload, thumbnail, attachment context, Vision/OCR, image generation,
  retouch, and returned Artifact preview flows.
- Prove progressive discovery and execution of shell/bash, read, fetch, CDP,
  OCR, Vision, and image capabilities under the local permission model.
- Produce signed Windows x64 and macOS arm64/x64 artifacts from one immutable
  source identity; verify SHA-256, Ed25519 signatures, supply-chain receipt, and
  public readback.
- Upgrade the installed Windows product through the public stable path and
  verify the desktop shortcut, password login, retained non-deleted legacy
  history/projects/artifacts, and exclusion of deleted sessions.
- Run installed-runtime acceptance with the real user session in Chrome.  On
  this Windows host, macOS acceptance is limited to reproducible artifact,
  structure, signature, and self-test evidence; native execution is not claimed.

## Authoritative current state

- Final candidate source version: `1.0.16`; it includes the historical Turn
  invariant convergence fix committed as `d4799aeb` and the previously merged
  visual/attachment closure from `2e3a86569`.
- Installed slot pointer is `1.0.15`, slot
  `r-6b1429946d57a325bc7a5fa0a01d999aaa1b563b`, with `1.0.14` retained as
  `previous`.  Activation is confirmed and the install-time launch reached a
  healthy listener, but a later desktop-shortcut cold launch fails during
  repeated legacy Skill convergence; therefore `1.0.15` is not the closure
  version.
- Public stable is temporarily `1.0.15`, release
  `release-stable-830675c67bc997411104d171`, build digest
  `830675c67bc997411104d171fd7c689dcb0194f1dbdc537c5ebe13992eb8de72`.
  The signed public pointer has authority sequence 16 and names the domestic
  GitHub mirror as the allowed stable source.  It must be superseded by the
  tested `1.0.16` fix before final acceptance.
- A local `1.0.12` build completed with build digest
  `b39c5c3dc8728ed5f4d9e574cbb2f5d9ae1b847429ba43eccdc32cef57836d97`.
  It is **not publishable** because it predates the current Runtime and visual
  fixes and has not passed installed real-user acceptance.
- The real account is already authenticated in the installed WebUI.  Bootstrap
  and the managed model catalog load, including GPT-5.6 SOL with the 272,000
  token compaction threshold and the canonical image model aliases.
- Installed `1.0.11` thread creation returns `503 RUNTIME_READ_ONLY`.  The
  exact audit found 606 `turn_projection_drift` violations and no other
  violation code: every affected migrated Turn had a matching, unique
  historical `turn.completed`, `turn.failed`, `turn.cancelled`, or
  `turn.interrupted` fact, but the auditor understood only the live
  `turn.status_changed` envelope.

## Root-cause changes already proved

- Commit `eddceb75b1a514b3f3bbececaa19ceee61645103` preserves `SYSTEMDRIVE`
  in the Windows supervisor environment and narrowly repairs the exact legacy
  Windows cache pollution pattern before full slot receipt validation.
- Commit `3561c036d483b366eb09a97519a3959b03d1f003` allows a newly signed target
  install to proceed after recovery observes an unrelated terminal failed
  transaction; it does not reuse a failed result as the target activation.
- Public `1.0.11` Bootstrap was downloaded from the first source, matched its
  manifest SHA-256, verified all fourteen components, installed successfully,
  created the desktop entry, and opened the browser.
- The installed UI projects migrated sessions, archived sessions, and projects.
  The stronger migration evidence remains the real-data audit in
  `development-log-v1.0.9-runtime-closure.md`.
- Visual/attachment closure was already merged in ancestor commit `2e3a86569`.
  Review confirmed authenticated bounded renditions, ready-state attachment
  thumbnails, managed visual evidence, image/retouch revisions, and concurrent
  rendition idempotency are present; the relevant backend, frontend contract,
  TypeScript, and Ruff checks passed.
- The current source auditor now recognizes the closed old terminal-event
  dialect only for Turn identities proven by `legacy_id_map`, validates the
  exact terminal payload, and continues to reject those event names on normal
  v1 Turns.  New imports emit the canonical `turn.status_changed` event from
  `accepted` to the historical terminal state.  Against the real installed
  database the audit changed from 606 violations to zero; two focused migration
  regressions and Ruff passed.  The running installed process remains correctly
  latched until it is restarted on a build containing this fix.
- The `1.0.13` release source was commit
  `54b7151fb5629e88e8825efcafc630c4ab9e5d1d`.  The `1.0.14` dependency-pack
  startup fix is commit `8be51055`, also pushed to `origin/main`.
  The three-platform build produced 43 signed artifacts and 46 publication
  files.  Manifest SHA-256 is
  `30ae501f9bc8335cd8631632d71baa0e84764147ecdd782a0a2a2395dbbb6941`;
  independent Ed25519/signature and supply-chain reports both passed.
- GitHub release `v1.0.13` and the production download/Admin pages are live.
  Public pointer readback returned HTTP 200, `no-store`, version `1.0.13`, and
  the exact release/build identities above.  The Windows public Bootstrap was
  independently downloaded, matched SHA-256
  `c0a8ec3beb46aa4af911e14fa33ab6313090c4a569188dd796994fd8516320ed`,
  and passed self-test.
- Combined selected backend regressions passed 134 checks except one existing
  50 ms scheduler assertion when run concurrently with the three-platform
  build; that test passed three consecutive isolated runs.  Web contract tests
  passed 204/204, TypeScript and production build passed, and the bundle gate
  passed.
- Real process-stack evidence for the `1.0.13` startup failure identified
  `VerifiedDependencyPackProcessAdapter._extract_verified_snapshot()` reading,
  expanding, and hashing the OCR dependency pack during synchronous HTTP app
  composition.  The signed OCR archive contains 4,138 entries; doing this for
  OCR and Office before binding the listener caused a false Bootstrap startup
  failure.  `1.0.14` defers verified snapshot materialization to the first
  actual dependency-service invocation.  It still verifies the source archive
  and complete extracted snapshot before execution and re-verifies both on
  subsequent invocations.  Four focused tests, Ruff, and 65 product-runtime /
  activation-health tests passed (one platform-specific skip).
- The complete `1.0.14` release was built from source commit
  `d45902665eb539286a375e12dd79e77785222392`.  It produced release ID
  `release-stable-8cb2f59ae1dc9dee61b5b65d`, build digest
  `8cb2f59ae1dc9dee61b5b65d1cdd30e34f1a3d3c02d307a04a1df52f8324e65f`,
  manifest SHA-256
  `8797a4fecd6d220d6b46ba5aaf22b70a6a741e096730699865ba2dbd848cff32`,
  and 46 GitHub assets whose names, byte sizes, signature report, and
  supply-chain report all matched.  Production readback returned version
  `1.0.14`, authority sequence 15, HTTP 200, and `no-store`.  The first-source
  Windows Bootstrap matched SHA-256
  `9e3039bf34739edb02119624619035e8e7f93bca37cae92f7e1ffb4b2fec4647`
  and passed self-test.
- A real public `1.0.14` install downloaded and verified all fourteen Windows
  components, displayed transfer rate and stage progress, installed the slot,
  migrated data, and confirmed activation.  Normal startup then exposed a
  second independent blocker: the persisted Extension state rejected a changed
  tool catalog even though the signed Core manifest had a new revision ID.
  The invariant was applied before the new-revision branch, effectively making
  every legitimate product update with catalog changes fail closed.  The
  `1.0.15` source restricts the immutable-negotiation check to reuse of the
  *same* revision; a new signed revision atomically advances active revision,
  catalog digest, and prior-known-good state.  A focused upgrade regression was
  added; the Extension suite passes 20 tests with one platform skip and Ruff is
  clean.
- The complete `1.0.15` release was built from source commit
  `65e1c5a93c2fd8ba681e02d19d7ba1d087b619ef`; its release ID is
  `release-stable-830675c67bc997411104d171`, build digest is
  `830675c67bc997411104d171fd7c689dcb0194f1dbdc537c5ebe13992eb8de72`,
  and manifest SHA-256 is
  `a8d43c8611391d58fd535f4949cc7844c1ac306c8dd58d6f55561479baddd904`.
  All 46 remote assets matched local names and byte sizes; independent
  signature and supply-chain verification passed; production readback returned
  authority sequence 16 and `no-store`.  The public Windows Bootstrap matched
  SHA-256 `7e92ea8861fd69c68968927a8ed251b1e1d845eb713f15193e7e5475a893fad6`
  and passed self-test.
- A real public `1.0.15` install verified all fourteen components, activated
  slot `r-6b1429946d57a325bc7a5fa0a01d999aaa1b563b`, opened port 8765, and preserved
  67 active plus 5 archived threads and 2 projects.  The installed auditor
  reported 3,716 events, 72 threads, 618 turns, 1,140 items, 12 jobs, 2
  interactions, and zero invariant violations.  A subsequent cold launch via
  the exact product desktop shortcut exposed another restart-only defect:
  legacy Skill import reused a revision-derived idempotency key while changing
  `expected_revision` after the first successful staging, so the second startup
  raised `ExtensionIdempotencyConflict`.  `1.0.16` now treats an already active
  or staged identical legacy revision as converged before entering the mutation
  path; changed legacy metadata still creates a new revision and request.
  Sixty-nine focused Extension/Product Runtime/dependency-pack tests pass with
  two platform skips, Ruff is clean, and the fixed source composed the installed
  real-data slot twice consecutively in 15.026 and 14.417 seconds without an
  idempotency conflict.

## Evidence still required

- Real public online update from installed `1.0.11` to the final version.
- Chrome evidence for reply/reasoning, attachments, OCR/Vision, image
  generation, retouch, shell/read/fetch, progressive discovery, and CDP.
- Windows shortcut/data-preservation proof after activation, and Windows-host
  structural verification of both public macOS Bootstrap artifacts.

## Next execution order

1. Build, sign, publish, and install `1.0.16`, then verify the activated slot,
   known-good predecessor, shortcut, listener, and data counts.
2. Run the complete real-user Chrome acceptance matrix and capture evidence.
3. Verify both public macOS artifacts from this Windows host without claiming a
   native macOS execution result.
4. Record the final evidence, run a last drift audit, and only then close the
   long Goal.

## Drift controls

- The public pointer, installed slot manifest, Git commit, candidate manifest,
  and test report must name the same final source/version before promotion.
- No source-only or API-only check substitutes for installed WebUI acceptance.
- No stale candidate is promoted after any tracked Runtime/Web/release change.
- User data, credentials, current/known-good slots, and deleted-session
  exclusion evidence are preserved during every recovery or cleanup action.

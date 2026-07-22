# e-Mate current release closure ledger

Last updated: 2026-07-22 (Asia/Shanghai)

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

- Final candidate source version: `1.0.14`; it includes the historical Turn
  invariant convergence fix committed as `d4799aeb` and the previously merged
  visual/attachment closure from `2e3a86569`.
- Installed slot pointer is `1.0.13`, slot
  `r-9b9847f47e2f6567d87960591705e8decb2e3935`, with `1.0.11` retained as
  `previous`.  The public update completed all fourteen downloads, signature
  checks, migration, activation, and shortcut creation, but the first normal
  Runtime launch exceeded its readiness window before opening port 8765.
- Public stable: `1.0.13`, release
  `release-stable-eb67d789bf936e39407ecea5`, build digest
  `eb67d789bf936e39407ecea56dec036f0a8a0c5d467f66bb3dcfb5692d6244dc`.
  The signed public pointer has authority sequence 14 and names the domestic
  GitHub mirror as the allowed stable source.
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
- The closure source is commit
  `54b7151fb5629e88e8825efcafc630c4ab9e5d1d`, pushed to `origin/main`.
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

## Evidence still required

- Real public online update from installed `1.0.11` to the final version.
- Chrome evidence for reply/reasoning, attachments, OCR/Vision, image
  generation, retouch, shell/read/fetch, progressive discovery, and CDP.
- Windows shortcut/data-preservation proof after activation, and Windows-host
  structural verification of both public macOS Bootstrap artifacts.

## Next execution order

1. Build, sign, publish, and install `1.0.14`, then verify the activated slot,
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

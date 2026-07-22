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

- Source version: `1.0.12` at commit
  `3561c036d483b366eb09a97519a3959b03d1f003` before the current uncommitted
  visual/attachment closure changes.
- Installed Runtime: `1.0.11`, current slot
  `r-803038457ed3b93b36d26dec78205751d1f2bc83`, serving loopback port 8765.
- Public stable: `1.0.11`, release
  `release-stable-4b4902ce406ec5e86c4aeef0`, build digest
  `4b4902ce406ec5e86c4aeef09ce19397d31a4c22cbc0d303dd02357e9ddb97a4`.
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
- Parallel visual closure work has focused tests for authenticated bounded
  renditions, attachment thumbnails, managed visual evidence, image/retouch
  revisions, and concurrent rendition idempotency.  These changes remain
  uncommitted and are not release evidence until reviewed and merged.
- The current source auditor now recognizes the closed old terminal-event
  dialect only for Turn identities proven by `legacy_id_map`, validates the
  exact terminal payload, and continues to reject those event names on normal
  v1 Turns.  New imports emit the canonical `turn.status_changed` event from
  `accepted` to the historical terminal state.  Against the real installed
  database the audit changed from 606 violations to zero; two focused migration
  regressions and Ruff passed.  The running installed process remains correctly
  latched until it is restarted on a build containing this fix.

## Evidence still required

- Green focused and broad Runtime/Web regressions after all parallel work is
  merged, plus clean lint/typecheck and release-source inventory.
- A new immutable final version build.  Do not reuse the stale `1.0.12` build.
- Public stable activation and exact readback of download page, Admin UI,
  release pointer, commands, assets, signatures, and digests.
- Real public online update from installed `1.0.11` to the final version.
- Chrome evidence for reply/reasoning, attachments, OCR/Vision, image
  generation, retouch, shell/read/fetch, progressive discovery, and CDP.

## Next execution order

1. Read the process-local invariant snapshot and reproduce the failure against
   a copy of the installed database.
2. Implement the narrow root fix with migration/recovery semantics and tests.
3. Review and merge all visual/attachment changes, then run the combined gates.
4. Bump once to the final version, build once, verify once, and publish once.
5. Update the installed product through the public path and run the complete
   real-user browser acceptance matrix.

## Drift controls

- The public pointer, installed slot manifest, Git commit, candidate manifest,
  and test report must name the same final source/version before promotion.
- No source-only or API-only check substitutes for installed WebUI acceptance.
- No stale candidate is promoted after any tracked Runtime/Web/release change.
- User data, credentials, current/known-good slots, and deleted-session
  exclusion evidence are preserved during every recovery or cleanup action.

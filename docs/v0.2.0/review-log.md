# v0.2.0 Review Log

## 2026-06-24 Hotfix Review: Send/Interrupt/Artifact UX

Hotfix gate used two independent read-only review agents. The implementation writer did not self-review.

| Agent | Slice | Result | Blocking Findings |
| --- | --- | --- | --- |
| Godel | UI state machine, repeated-message merge, artifact/media-before-`done` reconnect behavior | PASS | Two prior P1 findings fixed before PASS |
| Parfit | Release evidence, package/manifest/matrix freshness, test adequacy | PASS | No source P0/P1; final package/deploy steps required |

Resolved hotfix findings:

- P1: visible artifact/media output was marked `pending: false` before `done`, so reconnect could treat the assistant bubble as terminal and clear the active request.
  - Fix: added `visibleOutputSettled` local state, excluded it from terminal assistant detection, kept reconnect/stop controls visible, and cleared the flag only on true `done`/post-done terminal paths.
  - Coverage: `test_v020_send_attempt_uses_user_facing_copy_and_preserves_live_placeholder`.
- P1: `visibleOutputSettled` was initially not persisted, so reload could lose the guard and reintroduce premature terminal classification.
  - Fix: persisted `visibleOutputSettled` in both normal and minimal session-state serializers.
  - Re-review: Godel confirmed no remaining P0/P1 findings.

## 2026-06-24 Final Independent Review

Final gate used three independent read-only review agents. The implementation writer did not self-review.

| Agent | Slice | Result | Blocking Findings |
| --- | --- | --- | --- |
| Einstein | UI/UX, WebUI performance, packaged page smoke, manifest/package SHA parity | PASS | None |
| Leibniz | Runtime state merge, project/session persistence, regression-test adequacy | PASS | One prior P1 was fixed before final PASS |
| Boole | Cross-platform install, path/security scanning, Win/Mac package entry points | PASS | None |

## Resolved Finding

- P1: `save_ui_state` merge mode let existing `sessionProjects`, `pinnedProjects`, `sessionTitles`, and `pinnedSessions` values override explicit incoming updates.
  - Fix: merge-mode mappings now use incoming-preferred merge semantics while preserving omitted keys.
  - Coverage: `TestEcoreXWorkspaceState.test_ui_state_merge_updates_existing_mapping_values`.
  - Re-review: Leibniz confirmed the P1 is closed; Einstein and Boole also confirmed the final packages contain the corrected runtime.

## Final Consensus

All three review slices returned PASS with no remaining P0/P1 blockers. P2 notes are tracked as future hardening only and do not block v0.2.0 deployment.

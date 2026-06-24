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

## 2026-06-24 Hotfix Review: Model Config Admission

Hotfix gate used independent read-only review agents after deployment. The implementation writer did not self-review.

| Agent | Slice | Result | Blocking Findings |
| --- | --- | --- | --- |
| Meitner | Source/UI recovery copy, `/message` model-config response shape, package source scan | PASS | One prior P1 fixed before PASS |
| Nash | Production release, public manifest, package SHA parity, runtime version | PASS | None |
| Hypatia | Admin API client gate, user-token boundary, frontend draft recovery | PASS | None |

Resolved hotfix finding:

- P1: `/message` still had the old fallback copy `请先登录企业账号，或在设置 > 模型中配置可用的 API Key 后再发送。`
  - Fix: replaced the fallback with the account/model recovery copy, kept stable model-config error metadata, and added `assertNotIn` checks for both retired copies.
  - Coverage: `TestWebParallelHandlers.test_v020_webui_local_auth_falls_back_without_admin_client`.
  - Re-review: Meitner confirmed old prompts are gone from source/build paths; Nash and Hypatia confirmed production release and model-config/Admin API paths have no P0/P1 blockers.

Final model-config hotfix consensus: PASS.

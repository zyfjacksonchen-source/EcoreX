# EcoreX v0.1.15 Follow-up Iteration Log

## Start State

- Date: 2026-06-18
- Branch: `codex/ecorex-v0.1.15`
- Hand-test marker pushed: `v0.1.15-handtest-pass`
- Hand-test passed commit: `ff0bc81 docs: mark v0.1.15 handtest passed`
- Previous implementation commit: `cf38afb feat: ship v0.1.15 codex-like desktop UX`

## User-Reported Issues

1. Streaming still shows a large raw Markdown chunk, then only becomes formatted after the final output completes.
2. The command/request to view the latest local task log becomes unresponsive after the third conversation.

## Iteration Rules

- Make a rollback checkpoint before code changes.
- Keep implementation and review responsibilities separate across agents.
- Use parallel read-only agents for bug/performance/update/code-quality audits.
- Cross-check fixes until final review agents agree there are no blockers.
- Build to a local hand-testable Windows package.
- Open and test the packaged app locally, record observations, fix, and verify again.

## Running Notes

- 2026-06-18: Created follow-up iteration log after pushing `codex/ecorex-v0.1.15` and tag `v0.1.15-handtest-pass` to GitHub.

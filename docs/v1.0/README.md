# EcoreX v1.0 engineering record

This directory is the durable hand-off point for the v1.0 productization goal.
It is intentionally kept independent from transient chat context and generated
release artifacts.

- `implementation-log.md` records completed slices, current ownership, and the
  exact next recovery point.
- `decision-log.md` records architecture decisions that must not be silently
  reversed during later implementation.
- `verification-ledger.md` records commands, results, and known failures.
- `migration-inventory.md` records the v0.3.0 baseline and data that must be
  preserved or intentionally retired.
- `ga-gate-matrix.md` maps every product slice to executable evidence and the
  remaining release condition.
- `release-runbook.md` is the administrator's no-secret publication, promotion
  and incident procedure.
- `admin-management-runbook.md` records the user/quota/model/release workspace,
  hot model revision contract and the single-node secret/origin boundary.
- `responsive-ga-harness.md` documents the fixed-viewport, same-origin browser
  matrix used for responsive and accessibility release evidence.
- `image-intent-routing.md` records the effect-based, non-exclusive ImageGen
  routing contract, trust boundary and release gates.
- `progressive-capability-runtime.md` records the global Search/Describe/Grant/
  Admission boundary for Tools, Skills, MCP and Connector actions.
- `progress.json` is the machine-readable recovery pointer for long-running
  implementation sessions.

Every implementation batch must update the implementation log and verification
ledger before it is considered complete.

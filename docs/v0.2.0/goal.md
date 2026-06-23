# EcoreX v0.2.0 Goal

## Objective

EcoreX WebUI v0.2.0: based on the CowAgent comparison, fix Web responsiveness, knowledge/graph/channel discovery, and the bug where completed channel configuration cannot be discovered. Complete implementation, tests, parallel review, deployment, and durable evidence.

## Execution Rules

- Branch: `codex/ecorex-v0.2.0`
- Version marker changed from `v0.1.19` to `v0.2.0` on 2026-06-23.
- Rollback checkpoint before v0.2.0 work: `702072fa chore: checkpoint v0.1.19 long goal baseline`
- Keep WebUI-first release logic. Windows and macOS users install/update through WebUI local packages and manifest-verified scripts.
- Keep legacy v0.1.19 client keys as compatibility entries while v0.2.0 is rolling out.

## Non-Negotiable Acceptance Gates

- No infinite pending or continuing-thinking loop after stream/reconnect/tool failures.
- WebUI interactions are responsive on Windows and macOS: typing, switching sessions, deletion, folding, and streaming output.
- Projects and project sessions survive WebUI updates and do not drift into general sessions.
- Channel configuration must surface in `/api/channels`, `/api/extensions`, and the frontend capability/discovery UI.
- Knowledge list/read/graph endpoints must be discoverable and reachable from WebUI.
- Final closure requires independent parallel review consensus; writer does not review own work.

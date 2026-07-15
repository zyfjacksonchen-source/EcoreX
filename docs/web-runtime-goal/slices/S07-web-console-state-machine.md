# S7: Web Console State Machine

## Goal

Reduce Web console recovery debt by converging message submission, runtime recovery, and inline user actions onto one deterministic state machine. The Web UI should recover from refresh/SSE loss by reading runtime projections and active requests, and it should render repair/permission/model/connector prompts through the same inline action row contract.

## Changes

- Added `submitMessage(opts)` as the single Web console `/message` submit path.
- Routed `sendMessage`, `sendVoiceMessage`, and `regenerateResponse` through `submitMessage(opts)`.
- Added submit retry and de-duplicated failed-send recovery rendering through `renderSubmitFailureOnce()`.
- Added active request recovery via `/api/active-requests`.
- Kept runtime projection recovery via `/api/runtime-projection` and added rechecks on:
  - history load;
  - stream loss;
  - window focus;
  - visibility restore.
- Added `action_plans` to `RuntimeProjectionService` for:
  - `permission.requested` -> `confirm_permission`;
  - `capability.policy_blocked` -> `view_capability_policy`.
- Added frontend inline action row normalization/rendering for:
  - permission confirmation/denial;
  - model provider configuration;
  - capability repair/configuration rows;
  - connector login/configuration rows;
  - capability policy inspection.
- Added CSS for inline action rows in the existing console visual system.
- Fixed review-blocking edge cases:
  - terminal projections remove stale permission confirmation plans;
  - live SSE terminal refresh uses synchronized inline-row deletion rather than append-only rendering;
  - action-plan-only projections can create/render a bot recovery bubble;
  - submit-error permission rows now navigate to policy inspection instead of a missing permissions page;
  - failed submits clear the stale sending phase before rendering the recovery row.

## Boundaries

- Web-only: no Electron/desktop sidecar work is required for this slice.
- No new Web-only installer route was introduced. Inline repair rows surface deterministic actions and defer actual repair/install execution to the S3/S4 public installer and capability contracts.
- No new state source was introduced. The console reads runtime projection plus active request snapshots.
- Inline action payloads must not include raw permission arguments, provider keys, or secret-looking values.
- Permission decisions still go through `/api/tool-permissions` and the S5 broker policy; inline rows do not bypass permission checks.
- Image generation routing is unchanged and remains pinned to `gpt-image-2-pro`.

## Acceptance

- Exactly one frontend code path posts to `/message`.
- Voice send, text send, and regenerate all use the common submit pipeline.
- SSE stream loss first attempts runtime projection recovery, then active request recovery, before legacy fallback rendering.
- History load, focus, and visibility recovery read current runtime state.
- Permission and capability-policy events produce sanitized typed action rows.
- Terminal permission requests do not resurrect Allow/Deny buttons after completion, cancellation, or failure.
- Projection-only action plans render even when there are no tool calls and no assistant text yet.
- A failed submit produces at most one recovery/action prompt.
- Existing runtime projection Web regression tests continue to pass.

## Evidence

- `docs/web-runtime-goal/artifacts/S07-web-console-state-machine-tests.json`
- `docs/web-runtime-goal/reviews/S07-consensus.md`

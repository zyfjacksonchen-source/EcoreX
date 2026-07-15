# S06 Thinking Motion

## Status

Completed.

## Intent

Upgrade thinking indicators without adding motion dependencies.

## Decisions

- Main message flow: restrained pulse motion.
- Expanded details: staged icons for reasoning, searching, tooling, and artifact generation.
- Respect `prefers-reduced-motion`.

## Implementation

- Changed the main message `思考中` indicator from a static dot to a restrained pulse.
- Added staged step icons for expanded process details:
  - reasoning/thinking
  - searching/browsing
  - tooling
  - artifact/media generation
  - generic phase/status
- Classified phase/tool rows by text and tool name so the expanded details communicate the active work type.
- Kept motion CSS-only and dependency-free.
- Extended `prefers-reduced-motion` handling to disable step-icon animation.

## Verification

- `npm run typecheck`
- `npm run build:renderer`

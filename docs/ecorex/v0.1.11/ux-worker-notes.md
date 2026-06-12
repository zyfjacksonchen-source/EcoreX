# EcoreX v0.1.11 UX Worker Notes

## Scope
- Worker focus: desktop UX details, message disclosure, default persona guardrails, and latest-session shortcut.
- Edited only files allowed by the worker brief.
- Did not change agent core, runtime routing, or MCP/skill internals.

## Changes
- Added active-session styling state and `aria-current` for session rows.
- Kept session/project conversation summaries at normal weight, not bold.
- Added hover detail tooltips for key controls: new chat, latest shortcut, theme toggle, notification status, and session rows.
- Made the left “回到最新” action always useful: if there is no other newer session it scrolls the current chat to the latest message instead of becoming a dead button.
- Tightened `MessageContent` disclosure:
  - tool steps use `running` and `status` for live spinner state,
  - thinking/tool summaries have clear hover titles,
  - long assistant replies collapse by default with visible expand/collapse labels,
  - user messages are not collapsed by the long-reply rule.
- Added the missing `spin` keyframes so session/chat thinking indicators animate.
- Strengthened factory persona in both config templates: EcoreX must not self-identify as CowAgent or COW.

## Persona Audit
- Primary model persona path is `character_desc` in `config-template.json` and `desktop/runtime/ecorex-runtime/config-template.json`.
- Runtime model adapters still contain CowAgent/COW names in internal package/docs/logging paths. Those are core/internal compatibility names and were not edited in this worker.
- Potential override risk remains if a project-level `AGENT.md` or self-evolution memory has old CowAgent identity text. The main thread should handle this at the migration/runtime-default layer rather than patching core prompt code here.

## Verification
- Required worker check: `npm run typecheck` in `desktop`.
- Result is recorded in the worker final response.

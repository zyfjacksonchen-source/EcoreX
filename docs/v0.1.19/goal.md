# EcoreX v0.1.19 Goal

## Objective

Deliver a production-grade UX hardening iteration for Desktop/Web chat surfaces:
artifact availability, clipped menus, Codex-like reconnect/recovery, interrupt
send behavior, right-click add-to-chat, hidden Run Center, and collapsible
sidebar groups.

## Baseline

- Branch: `codex/ecorex-v0.1.19`
- Rollback baseline commit: `c63b514 chore: baseline before v0.1.19`
- Pre-existing `desktop/scripts/stage-runtime-win.ps1` diff was no longer present
  when execution started; the worktree was clean before the baseline commit.

## Requirements

- Do not show local deliverables that cannot be verified/opened.
- Reconnect and retry behavior must follow Codex-style safety: recover first,
  never duplicate tool execution automatically when state is uncertain.
- Sending while a task is running uses explicit interrupt-and-send semantics.
  The newest accepted user input wins; failed admission must not leave phantom
  persisted-looking turns.
- Run Center must be hidden from ordinary UI by default while backend recovery
  truth remains available.
- Project and general session lists must be collapsible and persisted.
- Windows, macOS, and Web/Electron fallback behavior must remain compatible.
- Acceptance requires independent parallel multi-agent review; the writing agent
  does not count as reviewer.

## Added Discussion Item

- Image generation must not silently fall back to Python/PIL/matplotlib/browser
  screenshots when the user asks for AI image creation. Parallel read-only
  investigation concluded that a production fix should hard-route
  `ai_image_generation` intent to a typed OpenAI image generation path targeting
  `gpt-image-2-pro`, keep Python only for explicit code visualization or
  post-processing, and fail closed with clear diagnostics when model/provider
  configuration is unavailable.
- The bundled and currently installed `create-xiaohongshu-note` skill must
  generate final cover/carousel images directly with `gpt-image-2-pro`, not
  draft/placeholder/Python images and not an automatic `gpt-image-2` fallback.
- The bundled and currently installed general `image-generation` skill must
  default to `gpt-image-2-pro` and fail closed on OpenAI model/access
  unavailability instead of silently falling back to `gpt-image-2` or another
  provider family.

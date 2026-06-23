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

## 2026-06-23 WebUI Follow-Up Goal

After the first WebUI-first v0.1.19 deployment, user testing found three
production blockers to close before the next public refresh:

- Windows WebUI project-folder selection opens a native system picker, but the
  picker can appear behind the browser and the page showed no pending state.
- macOS WebUI online installer can fail on Apple Silicon and Intel alike when
  `/bin/bash` 3.2 expands an empty array under `set -u`; package relaunch must
  also avoid port drift when an old service survives without a pid file.
- Feishu/Lark capability state, local CLI discovery/auth, and the model-visible
  `feishu_cli` structured tool must be aligned so raw `lark-cli` remains blocked
  while the safe structured path is always available.

Acceptance for this follow-up remains production-grade: source and packaging
scripts must both be patched, WebUI packages redeployed, GitHub release assets
updated, and independent read-only review agents must reach PASS consensus.

## 2026-06-23 WebUI Performance / Reconnect Structural Goal

Additional user testing found that the WebUI-first build still felt sluggish on
Windows and macOS: typed text appeared late, switching/deleting/folding sessions
lagged, and streaming output did not feel real-time compared with WorkBuddy on
the same machines. The network recovery bubble also looked like a dead end when
the SSE channel was briefly interrupted.

Production criteria for this slice:

- Composer typing must not drive whole-app React state updates on every key.
- Streaming deltas must be coalesced before crossing SSE and React rendering
  boundaries, while preserving real-time feel and terminal ordering.
- Runtime snapshots must avoid re-fetching heavy capability/tool/skill data on
  every lightweight UI refresh.
- History pagination must load the requested window instead of scanning and
  slicing full long-session histories.
- EventSource transient errors must let the browser reconnect to the same run
  before the UI surfaces a manual recovery bubble; no automatic duplicate
  execution is allowed when state is uncertain.
- Ordinary WebUI must not show Run Center entry points or user-facing Run Center
  copy by default.

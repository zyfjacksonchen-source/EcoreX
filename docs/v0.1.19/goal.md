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

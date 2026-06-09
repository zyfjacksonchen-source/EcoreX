# EcoreX Enterprise Implementation Plan

Date: 2026-06-09
Target: v0.1.1

## Success Criteria

- Desktop/web UI presents EcoreX branding and no user-facing Claude Code / cc-haha branding.
- Only light and dark EcoreX themes are available; old theme values migrate to light.
- Enterprise login gate blocks the workbench until a valid enterprise session exists.
- Default admin can log in, must change the bootstrap password, and can manage enterprise users.
- Admin page supports user lifecycle, role changes, daily token limits, usage review, audit log review, provider setup, and version push policy.
- Members cannot see provider/API key configuration and cannot call provider write APIs.
- Runtime sessions use the active enterprise provider automatically.
- Daily token quota blocks member actions after the configured limit with a clear 429 state.
- Version identity is v0.1.1 where desktop package/native release metadata is user-visible.

## Implementation Checklist

1. Add durable docs for spec, implementation plan, and admin operator guide.
2. Add enterprise persistence service under app-owned `~/.claude/cc-haha`.
3. Add enterprise auth, users, usage, audit, provider, and version policy APIs.
4. Protect provider/settings write paths with enterprise admin checks.
5. Require enterprise session context for protected REST and WebSocket agent paths.
6. Record per-user daily token usage and block over-quota member actions.
7. Add frontend enterprise API client and auth store.
8. Add login/password-change gate before the workbench.
9. Add admin-only page/tab for users, quotas, usage, logs, provider, and version policy.
10. Hide provider/API key self-service from ordinary members.
11. Collapse theme model to `light | dark` and apply orange-led EcoreX tokens.
12. Rebrand desktop UI, notification/about/update surfaces, package metadata, and visible app shell copy.
13. Add/update server, persistence, and desktop tests.
14. Re-check Figma tools; if unavailable, record blocker and run browser visual smoke.
15. Run targeted gates, then wider verification when practical.

## Verification Plan

Narrow checks:

- Server enterprise API/service tests.
- Desktop enterprise auth/admin/theme tests.
- Persistence upgrade tests for enterprise defaults and theme migration.

Repository gates requested:

- `bun run check:server`
- `bun run check:desktop`
- `bun run check:persistence-upgrade`
- `bun run verify`
- `bun run check:native` after version/native title updates

## Figma Status

The implementation must re-check callable Figma tool availability before visual handoff. If no Figma tools are exposed in the current Codex session, record that as an execution blocker and proceed with browser-based visual QA.

2026-06-09 update: Re-checked dynamic tools with `tool_search` for `use_figma`, `create_new_file`, and Figma MCP actions. No callable Figma write/debug tool was exposed in this thread, so Figma frame generation is blocked by tool availability. Continue with browser-based visual QA and keep this blocker in handoff.

## Rollback Notes

The enterprise state is stored separately from user-owned global Claude settings. Reverting frontend/server code should leave enterprise data files inert. Provider runtime remains on the existing app-owned provider service path so runtime compatibility is preserved.

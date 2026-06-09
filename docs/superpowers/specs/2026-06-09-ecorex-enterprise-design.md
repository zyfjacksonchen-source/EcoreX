# EcoreX Enterprise Design Spec

Date: 2026-06-09
Version target: v0.1.1
Surface: desktop, server, provider/runtime, persistence, native/release, docs

## Product Identity

EcoreX is an enterprise AI Agent by Yixin R&D for advertising agencies. It supports recurring agency work such as reports, media plans, creative concepts, Xiaohongshu notes, and data analysis.

The user-facing product must not expose the original Claude Code, Claude Code Haha, Claude Code Companion, or cc-haha branding in the desktop/web UI. Internal filenames and legacy storage keys can remain when they are not user-visible and changing them would risk runtime compatibility.

## Visual Direction

The EcoreX interface uses one orange-led enterprise visual system with two color modes only:

- `light`: bright EcoreX enterprise workspace.
- `dark`: dark EcoreX enterprise workspace.

Legacy theme values `white` and old `light` migrate to `light`. The previous three-mode visual selector is removed from the frontend.

The UI should feel operational and B2B-focused: dense, readable, calm, and suitable for repeated advertising workflow tasks. It should avoid marketing hero composition inside the product shell.

## Enterprise Auth

Enterprise mode stores app-owned data under `~/.claude/cc-haha` and does not mutate user-owned global Claude settings.

Default bootstrap administrator:

- Email: `admin@ecorex.local`
- Password: `EcoreX@2026!ChangeMe`
- First login: must change password before entering the workbench.

Only salted password hashes and session token hashes are persisted. Raw passwords and raw session tokens are never written to disk or returned by admin read endpoints.

## Roles

Roles:

- `admin`: can manage users, provider configuration, quotas, audit logs, and version policy. Admin usage is unlimited by default.
- `member`: can use the agent if active and within token quota. Members cannot see or write provider/API key configuration.

User status:

- `active`: login and agent use allowed.
- `disabled`: login and agent use blocked.

## Admin Web Page

Admins get a dedicated Admin page in the desktop web shell. The page covers:

- Enterprise users: create, disable/enable, reset password, assign role, set daily token limits.
- Usage: user-level daily token usage summaries.
- Audit log: login, logout, password changes, user changes, provider/API key changes, quota changes, quota blocks, and version policy changes.
- Provider: unified enterprise provider/API key setup.
- Version policy: target version, push message, and force/recommend state.

Members must not see the Admin page or provider/API key controls.

## Provider Governance

The existing provider runtime path remains the source of runtime environment configuration. Enterprise mode moves write access to admin APIs only:

- Admin configures the unified provider/API key from the Admin page.
- Existing provider/settings write endpoints reject non-admin requests.
- Runtime sessions use the configured active enterprise provider automatically.

## Token Quotas

Usage is tracked by enterprise user and local server date.

Token total is:

`input + output + cache_read + cache_creation`

Members use `dailyTokenLimit`; `null` means unlimited. Admin users are unlimited by default. If a member reaches the daily limit, the next agent action is blocked with HTTP 429 / WebSocket error state and a clear frontend limit message.

## Public API Groups

Enterprise APIs:

- `/api/enterprise/auth/*`
- `/api/enterprise/users/*`
- `/api/enterprise/usage`
- `/api/enterprise/audit-log`
- `/api/enterprise/provider`
- `/api/enterprise/version-policy`

Once enterprise mode is initialized, protected REST and WebSocket agent paths require enterprise session context.

## Figma And QA

Requested Figma deliverables include login, main shell, admin dashboard, users, usage, logs, permissions, and provider setup in light/dark. If callable Figma tools are unavailable in the current Codex context, the blocker must be recorded and browser screenshot QA should be used instead.

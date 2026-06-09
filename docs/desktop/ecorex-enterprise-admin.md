# EcoreX Enterprise Admin Guide

Date: 2026-06-09
Target version: v0.1.1

## Bootstrap Login

Default administrator:

- Email: `admin@ecorex.local`
- Password: `EcoreX@2026!ChangeMe`

The first successful bootstrap login requires a password change. The default password should be treated as a temporary setup secret only.

## Admin Responsibilities

Admins manage enterprise access from the Admin page:

- Create enterprise users.
- Enable or disable users.
- Reset user passwords.
- Assign `admin` or `member` roles.
- Set each member's daily token limit.
- Review daily token usage.
- Review audit logs.
- Configure the unified enterprise provider/API key.
- Set the version push policy shown to enterprise users.

## Provider/API Key Policy

Provider and API key configuration is administrator-only. Ordinary enterprise users do not choose providers or enter API keys in the frontend. Agent sessions use the active enterprise provider configured by an admin.

## Daily Token Limits

Admins are unlimited by default. Members use their `dailyTokenLimit`; an empty limit means unlimited.

Daily usage is counted by local server date. Total tokens are calculated as:

`input + output + cache_read + cache_creation`

When a member reaches the configured daily limit, the next agent action is blocked with a clear quota message.

## Audit Events

The audit log records security and governance events including:

- Login and logout.
- Password change and reset.
- User creation, disable/enable, role change, and quota change.
- Provider/API key changes.
- Quota blocks.
- Version policy changes.

Audit records should not expose raw passwords, raw session tokens, or raw API keys.

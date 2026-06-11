# EcoreX v0.1.10 Productization Notes

## Goal

EcoreX v0.1.10 turns the current desktop/admin demo into a usable enterprise AI Agent release:

- Admin-created users can log in with email and password.
- Admins can reset passwords, disable users, set daily/weekly token limits, inspect usage, and review errors by user and device.
- The desktop app opens into a compact Codex-style two-column interface and works after install without manual local runtime configuration.
- Heavy capability packs are installed on first use or prepared by admins, with clear progress and failure feedback.

## Non-Goals

- macOS signing, notarization, and Gatekeeper validation are not part of this Windows implementation round.
- The agent core remains compatible and should not be refactored for branding-only changes.

## Current Risks

- Public `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned HTTP 404 during the latest verification retry; local v0.1.10 artifacts are ready but not deployed publicly.
- Provider-returned token usage is normalized when present; estimated token usage remains as fallback for streaming providers that omit usage.
- macOS signing, notarization, Gatekeeper, and installed smoke remain deferred to the later Mac environment.

## Recovery Checklist

When resuming this goal, read in order:

1. `docs/ecorex/goal-ledger.md`
2. `docs/ecorex/v0.1.10/admin-policy-contract.md`
3. `docs/ecorex/v0.1.10/runtime-packaging.md`
4. `docs/ecorex/v0.1.10/acceptance-log.md`
5. `docs/ecorex/v0.1.10/public-deployment-runbook.md`

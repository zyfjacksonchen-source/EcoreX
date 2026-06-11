# EcoreX v0.1.10 Admin and Client Contract

## User Contract

- Admin creates users with `name`, `email`, `initialPassword`, `role`, optional `dailyTokenLimit`, and optional `weeklyTokenLimit`.
- Users log in from the desktop app with email and password.
- Login returns a short-lived user token, user profile, quota state, model policy metadata, and device identity binding.
- Admin can edit user name, email, role, status, daily/weekly limits, and reset password.
- Delete is implemented as soft delete/disabled status to preserve historical usage and error traceability.

## Quota Contract

- Daily and weekly limits are token counts.
- Before sending a chat request, the desktop checks quota through the admin API.
- If a user is over daily or weekly limit, the desktop blocks the send and displays a user-readable message above the composer.
- Usage events include user email, device ID, session ID, model, input tokens, output tokens, total tokens, and detail JSON when available.
- Legacy usage events without token detail count through `amount` for backward compatibility.

## Error Contract

- Desktop error events include user email, device ID, app version, session ID, source, tool, message, and detail JSON.
- Admin can filter error logs by user, device, level, and time window.
- Error details must be visible in the Admin Web without relying only on browser `title` tooltips.

## Model Contract

- Admin Web exposes one global enterprise model configuration.
- Create/delete model controls are hidden from the UI.
- The existing `model_credentials` table may remain for compatibility, but the client receives only the active global policy.
- Editing the model happens in a modal and preserves the previous API key when the key field is left blank.
- Desktop model policy delivery requires both the shared client key and a valid enterprise user token. A client key alone must return 401 for `/client/model-config`; this prevents deleted/disabled users and over-quota bypasses from continuing to receive model credentials.
- Renderer code must never receive or persist the enterprise user token. The Electron main process owns the token and exposes only a sanitized session view to the UI.
- The default client key `ecorex-desktop-v0.1.10` is a public desktop channel marker for out-of-box installs, not a secret. Deployments may override it with `ECOREX_CLIENT_EVENT_KEY`, but model credentials must continue to require an authenticated user token.

## Route Contract

- Public/static Admin page path may be `/admin/` or `/ecorex-agent/admin/`.
- Admin API normalizes `/admin/api/*`, `/api/admin/*`, `/ecorex-agent/admin/api/*`, and `/ecorex-agent/api/admin/*` to the same internal routes.
- Desktop client routes remain under `/client/*` or `/ecorex-agent/client/*`.

## Capability Policy Contract

- Capability packs support `ask`, `preinstall`, and `disabled`.
- `ask`: desktop may install on first use after user confirmation.
- `preinstall`: admin intends the pack to be ready; desktop still reports missing dependencies and offers repair if absent.
- `disabled`: ordinary users cannot install the pack.

## Release Verification Contract

- `scripts/verify-ecorex-release.ps1` verifies public manifest/download status and skips macOS artifacts by default for this Windows round.
- Pass `-ClientEventKey` to verify client-key protected endpoints.
- Pass both `-ClientEventKey` and `-ClientUserToken` to verify authenticated model policy delivery, because model credentials must not be returned to key-only callers.

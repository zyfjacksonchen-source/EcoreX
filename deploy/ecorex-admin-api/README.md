# EcoreX Admin API

Lightweight zero-dependency Admin API for the EcoreX download/admin site.

- Runtime: Python stdlib only.
- Storage: SQLite.
- Intended path: protected by Caddy Basic Auth at `/ecorex-agent/admin/api/*`.
- Local/root static-site compatibility: `/admin/api/*` and `/api/admin/*` are also normalized to the same API routes so `deploy/ecorex-site/admin/` can run from a normal `/admin/` path.
- Data directory: `/srv/ecorex-agent-admin/data`.
- Client desktop channel key defaults to `ecorex-desktop-v0.1.19` and also accepts the v0.1.10-v0.1.18 desktop keys plus `ecorex-web-v0.1.11-web.1` through `ecorex-web-v0.1.19-web.1` during rollout when `ECOREX_CLIENT_EVENT_KEYS` is not set. Treat these as public app channel markers, not as secrets. Model credentials still require an authenticated enterprise user token.
- To test a v0.1.19 client against an older admin deployment, the v0.1.19 Desktop/WebUI clients will retry with compatible public channel keys when the server returns `invalid client key`. Deploying this admin API revision is still recommended before release because it accepts the new v0.1.19 keys directly while keeping older keys enabled.

The API keeps product admin state separate from the static download release path and from the CowAgent agent core.

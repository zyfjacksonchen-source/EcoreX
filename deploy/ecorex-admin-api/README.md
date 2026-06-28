# EcoreX Admin API

Lightweight zero-dependency Admin API for the EcoreX download/admin site.

- Runtime: Python stdlib only.
- Storage: SQLite.
- Intended path: protected by Caddy Basic Auth at `/ecorex-agent/admin/api/*`.
- Local/root static-site compatibility: `/admin/api/*` and `/api/admin/*` are also normalized to the same API routes so `deploy/ecorex-site/admin/` can run from a normal `/admin/` path.
- Data directory: `/srv/ecorex-agent-admin/data`.
- Client channel key defaults to the v0.2.2 Web marker `ecorex-web-v0.2.2-web.1` and also accepts the v0.2.1 Web marker, the v0.2.0 desktop key, v0.1.10-v0.1.19 desktop keys, and `ecorex-web-v0.1.11-web.1` through `ecorex-web-v0.2.0-web.1` during rollout when `ECOREX_CLIENT_EVENT_KEYS` is not set. Treat these as public app channel markers, not as secrets. Model credentials still require an authenticated enterprise user token.
- To test a v0.2.2 Web client against an older admin deployment, the Web client will retry with compatible public channel keys when the server returns `invalid client key`. Deploying this admin API revision is still recommended before release because it accepts the new v0.2.2 Web key directly while keeping older keys enabled.
- Admin Basic Auth accepts `ECOREX_ADMIN_USERNAME` for one username, or `ECOREX_ADMIN_USERNAMES=admin,root` for a comma-separated allow-list that shares `ECOREX_ADMIN_PASSWORD`.

The API keeps product admin state separate from the static download release path and from the CowAgent agent core.

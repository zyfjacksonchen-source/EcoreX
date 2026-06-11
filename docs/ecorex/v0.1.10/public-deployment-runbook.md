# EcoreX v0.1.10 Public Deployment Runbook

## Current State

- Local release is ready.
- Public release is not yet updated: `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned HTTP 404 during the latest 2026-06-11 verification retry after proxy was enabled. Earlier verification had seen v0.1.7.
- Windows installer is signed and locally smoke-tested:
  - `desktop/release/EcoreX_0.1.10_x64-setup.exe`
  - size `117,529,360`
  - SHA256 `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`

## Files To Publish

Preferred handoff artifact:

- `release-artifacts/EcoreX_0.1.10-public-release.zip`
- size `120,277,051`
- SHA256 `EAD857656A7399DCCC7D5052049DF889D22BA0C4B38D25658DA04CB7D76571F1`

Generate or refresh it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prepare-ecorex-public-release.ps1
```

The zip contains:

- `site/**`
- `site/downloads/EcoreX_0.1.10_x64-setup.exe`
- `admin-api/ecorex_admin_api.py`
- `admin-api/Dockerfile`
- `admin-api/README.md`
- `server/install-ecorex-public-release.sh`
- `server/check-ecorex-server-release.sh`
- `server/caddy/Caddyfile.example`
- `server/caddy/ecorex-agent.routes.caddy`
- `server/nginx/ecorex-agent.conf.example`
- `server/systemd/ecorex-admin-api.service.example`
- `checksums.json`

It intentionally excludes older installers, SQLite files, pycache, local runtime folders, and development build output.

Git handoff artifacts, useful when the current machine cannot push to GitHub:

- `release-artifacts/EcoreX_0.1.10-productization.bundle`
  - size `6,518,904`
  - SHA256 `B53B9CBB8276E9D5FF1D9A589571FA565D605DD9AD74730C27901A0BFE611A1A`
  - contains branch `codex/ecorex-v0.1.10-productization` at commit `f7861062df11b88aa783ff1e736ca92253532363`
- `release-artifacts/EcoreX_0.1.10-productization.patch`
  - size `15,109,034`
  - SHA256 `A2D8B5731648D2566F6E7571A8540A1DF0A8B732A29CDCAEA3BA83C2ABC0AA9C`

Import the bundle on a machine with GitHub access:

```powershell
git clone git@github.com:zhangyifanjackson-dotcom/EcoreX.git EcoreX
cd EcoreX
git fetch C:\CowAgent\release-artifacts\EcoreX_0.1.10-productization.bundle codex/ecorex-v0.1.10-productization:codex/ecorex-v0.1.10-productization
git switch codex/ecorex-v0.1.10-productization
git push origin codex/ecorex-v0.1.10-productization
```

If SSH is unavailable on the current Windows machine because `git@github.com` returns `Permission denied (publickey)`, use HTTPS token push or the bundle import path above.

Raw source paths:

- Static site:
  - `deploy/ecorex-site/index.html`
  - `deploy/ecorex-site/site.js`
  - `deploy/ecorex-site/styles.css`
  - `deploy/ecorex-site/manifest.json`
  - `deploy/ecorex-site/assets/**`
  - `deploy/ecorex-site/admin/**`
- Caddy route template:
  - `deploy/ecorex-site/caddy/Caddyfile.example`
  - `deploy/ecorex-site/caddy/ecorex-agent.routes.caddy`
- Windows artifact:
  - `deploy/ecorex-site/downloads/EcoreX_0.1.10_x64-setup.exe`
- Admin API:
  - `deploy/ecorex-admin-api/ecorex_admin_api.py`
  - `deploy/ecorex-admin-api/Dockerfile`
- Server deployment helpers:
  - `scripts/install-ecorex-public-release.sh`
  - `scripts/check-ecorex-server-release.sh`
  - `deploy/ecorex-site/nginx/ecorex-agent.conf.example`
  - `deploy/ecorex-admin-api/systemd/ecorex-admin-api.service.example`

The local downloads directory may contain older installers. Only files referenced by the v0.1.10 `manifest.json` need to be exposed as current release artifacts.

## Server Target

Use the existing isolated EcoreX paths:

- Static release root: `/srv/ecorex-agent-download/releases/<timestamp>-v0.1.10`
- Current symlink: `/srv/ecorex-agent-download/current`
- Admin API data: `/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3`
- Public route: `https://www.ecoreai.cn/ecorex-agent/`
- Admin route: `https://www.ecoreai.cn/ecorex-agent/admin/`
- Admin API route: `https://www.ecoreai.cn/ecorex-agent/admin/api/*`
- Client routes:
  - `https://www.ecoreai.cn/ecorex-agent/client/auth/login`
  - `https://www.ecoreai.cn/ecorex-agent/client/model-config`
  - `https://www.ecoreai.cn/ecorex-agent/client/capability-policy`
  - `https://www.ecoreai.cn/ecorex-agent/client/events`

## Admin API Environment

Set on the server service:

```bash
ECOREX_ADMIN_DB=/data/ecorex-admin.sqlite3
ECOREX_CLIENT_EVENT_KEY=ecorex-desktop-v0.1.10
ECOREX_ALLOWED_ORIGINS=https://www.ecoreai.cn
ECOREX_ADMIN_USERNAME=admin
ECOREX_ADMIN_PASSWORD=<set-a-strong-password>
# Or use token/key auth for Admin API automation:
# ECOREX_ADMIN_TOKEN=<set-a-strong-token>
# ECOREX_ADMIN_API_KEY=<set-a-strong-key>
```

The client event key is a public desktop channel marker, not a model/API secret. Model credentials are released only after ordinary user login and valid enterprise user token validation.

Do not enable default demo users in production. `ECOREX_SEED_DEFAULT_USERS` and `ECOREX_ALLOW_DEFAULT_USERS` are for controlled local/demo recovery only, and require explicit environment configuration.

## Deployment Steps

Preferred scripted deployment on the Linux server:

```bash
export EXPECTED_SHA256=EAD857656A7399DCCC7D5052049DF889D22BA0C4B38D25658DA04CB7D76571F1
bash scripts/install-ecorex-public-release.sh /path/to/EcoreX_0.1.10-public-release.zip
```

The script verifies the zip, creates a new `/srv/ecorex-agent-download/releases/<timestamp>-v0.1.10` directory, copies Admin API files to `/srv/ecorex-agent-admin/app`, copies server helper scripts/configs to `/srv/ecorex-agent-admin/server`, preserves old releases, and atomically updates `/srv/ecorex-agent-download/current`.

After installation, run the server-side check:

```bash
CHECK_PUBLIC=1 CHECK_CADDY=1 bash /srv/ecorex-agent-admin/server/check-ecorex-server-release.sh
```

Before the Caddy route is reloaded, this check is expected to fail public static route checks. After the Caddy file_server mapping is applied, it should pass `manifest`, `root`, `admin auth`, and `client API gate`.

If only the zip is present on the server, extract the helper first:

```bash
unzip -j EcoreX_0.1.10-public-release.zip server/install-ecorex-public-release.sh -d /tmp/ecorex-release
export EXPECTED_SHA256=EAD857656A7399DCCC7D5052049DF889D22BA0C4B38D25658DA04CB7D76571F1
bash /tmp/ecorex-release/install-ecorex-public-release.sh EcoreX_0.1.10-public-release.zip
```

Manual deployment equivalent:

1. Create a new server release directory.
2. Unzip `release-artifacts/EcoreX_0.1.10-public-release.zip` on the server, or copy the equivalent raw files listed above.
3. Copy `site/**` into the new static release directory.
4. Update/redeploy the Admin API from `admin-api/ecorex_admin_api.py`.
5. Configure Admin credentials and allowed origins before exposing `/admin/api/*`.
6. Ensure `manifest.json` contains the new hash and `status: ready` for `windows-x64`.
7. Update `/srv/ecorex-agent-download/current` to point to the new release directory.
8. Keep the previous release directory and previous Admin API image/script available for rollback.

Caddy is the observed public server for `www.ecoreai.cn`. If the site block already exists, import `deploy/ecorex-site/caddy/ecorex-agent.routes.caddy` inside the existing `www.ecoreai.cn` block. Use `deploy/ecorex-site/caddy/Caddyfile.example` only as a complete-site reference. API routes should reverse proxy to `127.0.0.1:18084`, while `/ecorex-agent/*` should use `file_server` rooted at `/srv/ecorex-agent-download/current`. Run `caddy validate` before reload.

Nginx routing is also documented for alternate deployments. Use `deploy/ecorex-site/nginx/ecorex-agent.conf.example` only if the host is migrated to Nginx, and run `nginx -t` before reload.

If systemd is used for the Admin API, copy `deploy/ecorex-admin-api/systemd/ecorex-admin-api.service.example` to `/etc/systemd/system/ecorex-admin-api.service`, create the `ecorex` service user if needed, edit `/srv/ecorex-agent-admin/env/ecorex-admin-api.env`, then run:

```bash
systemctl daemon-reload
systemctl enable --now ecorex-admin-api.service
systemctl status ecorex-admin-api.service
```

## Verification

Run from this workspace after deployment:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-ecorex-release.ps1 `
  -LocalWindowsInstaller desktop\release\EcoreX_0.1.10_x64-setup.exe `
  -ClientEventKey ecorex-desktop-v0.1.10 `
  -SkipGitRemoteCheck
```

Before deployment, or while the public route is still being wired, run the local/package acceptance harness:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test-ecorex-v0.1.10-acceptance.ps1 `
  -AllowPublicBlocked `
  -ExpectedGitHubCommit <current-remote-snapshot-sha>
```

This checks the local manifest, signed Windows installer, public release zip contents, Linux-safe zip paths, temp install layout, GitHub refs, and the current public route status. It should only report `Public static route` as blocked before the production `/ecorex-agent` route is deployed.

When verifying GitHub source sync in the same pass, omit `-SkipGitRemoteCheck` and pass the current remote snapshot commit. This is needed because the Windows handoff may update GitHub through a clean snapshot/API commit whose SHA differs from the local shallow CowAgent commit:

```powershell
$remoteHead = (git ls-remote https://github.com/zhangyifanjackson-dotcom/EcoreX.git refs/heads/main) -split "\s+" | Select-Object -First 1
powershell -ExecutionPolicy Bypass -File scripts\verify-ecorex-release.ps1 `
  -LocalWindowsInstaller desktop\release\EcoreX_0.1.10_x64-setup.exe `
  -ClientEventKey ecorex-desktop-v0.1.10 `
  -ExpectedGitHubCommit $remoteHead
```

If `git ls-remote` is unstable on the current network, set `ECOREX_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN` in the shell before running the verifier. The script will fall back to GitHub's refs API for the GitHub sync check.

Expected:

- Public manifest product/version passes with `EcoreX 0.1.10`.
- Windows public download returns HTTP 200.
- Local installer hash matches the public manifest hash.
- Authenticode signature is valid.
- Admin routes return 401 without admin auth.
- Unauthenticated model/capability routes return 403 or 401 depending on route and missing credential.
- Client-key-only model config returns 401.
- Invalid or over-quota user sessions do not receive model credentials.
- If `-ExpectedGitHubCommit` is used, remote `main` and `codex/ecorex-v0.1.10-productization` both match that commit.
- macOS artifact checks are skipped in this Windows round.

After creating a real ordinary user, pass `-ClientUserToken` to verify authenticated model policy delivery.

## Rollback

1. Point `/srv/ecorex-agent-download/current` back to the previous release directory.
2. Restart/redeploy the previous Admin API service if API migration issues appear.
3. Re-run `scripts\verify-ecorex-release.ps1` against the restored version.

Do not roll back by deleting the v0.1.10 release directory until its logs and deployed artifacts have been archived.

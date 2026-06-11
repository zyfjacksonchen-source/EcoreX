# EcoreX v0.1.10 Public Deployment Runbook

## Current State

- Local release is ready.
- Public release is not yet updated: `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned v0.1.7 during the 2026-06-11 verification pass.
- Windows installer is signed and locally smoke-tested:
  - `desktop/release/EcoreX_0.1.10_x64-setup.exe`
  - size `120,050,856`
  - SHA256 `14D57A4F15D2F99DDC04975D5E636707F648864665D4F3F4D5A011516626DB55`

## Files To Publish

Preferred handoff artifact:

- `release-artifacts/EcoreX_0.1.10-public-release.zip`
- size `122,791,331`
- SHA256 `CE05311BE1FE949ACA1483349EC543E5A945C4D45211DF533EA1AA0B6F068429`

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
- Windows artifact:
  - `deploy/ecorex-site/downloads/EcoreX_0.1.10_x64-setup.exe`
- Admin API:
  - `deploy/ecorex-admin-api/ecorex_admin_api.py`
  - `deploy/ecorex-admin-api/Dockerfile`

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
```

The client event key is a public desktop channel marker, not a model/API secret. Model credentials are released only after ordinary user login and valid enterprise user token validation.

## Deployment Steps

1. Create a new server release directory.
2. Unzip `release-artifacts/EcoreX_0.1.10-public-release.zip` on the server, or copy the equivalent raw files listed above.
3. Copy `site/**` into the new static release directory.
4. Update/redeploy the Admin API from `admin-api/ecorex_admin_api.py`.
5. Ensure `manifest.json` contains the new hash and `status: ready` for `windows-x64`.
6. Update `/srv/ecorex-agent-download/current` to point to the new release directory.
7. Keep the previous release directory and previous Admin API image/script available for rollback.

## Verification

Run from this workspace after deployment:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-ecorex-release.ps1 `
  -LocalWindowsInstaller desktop\release\EcoreX_0.1.10_x64-setup.exe `
  -ClientEventKey ecorex-desktop-v0.1.10 `
  -SkipGitRemoteCheck
```

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
- Unauthenticated model/capability routes return 403.
- Client-key-only model config returns 401.
- If `-ExpectedGitHubCommit` is used, remote `main` and `codex/ecorex-v0.1.10-productization` both match that commit.
- macOS artifact checks are skipped in this Windows round.

After creating a real ordinary user, pass `-ClientUserToken` to verify authenticated model policy delivery.

## Rollback

1. Point `/srv/ecorex-agent-download/current` back to the previous release directory.
2. Restart/redeploy the previous Admin API service if API migration issues appear.
3. Re-run `scripts\verify-ecorex-release.ps1` against the restored version.

Do not roll back by deleting the v0.1.10 release directory until its logs and deployed artifacts have been archived.

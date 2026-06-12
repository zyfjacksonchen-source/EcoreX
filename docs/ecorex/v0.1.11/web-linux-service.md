# EcoreX v0.1.11 WebUI Linux Service Package

## Scope
- Adds a parallel `0.1.11-web.1` WebUI delivery path that does not change the existing desktop or Admin API release flow.
- Installs a full Python runtime service under `/opt/ecorex-web/current`.
- Uses `/srv/ecorex-agent-workspace` as the shared agent workspace.
- Defaults to `WEB_PORT=9909` and binds to `127.0.0.1` for reverse proxy deployment.
- Generates and persists `WEB_PASSWORD` in `/etc/ecorex-web/ecorex-web.env`.
- Writes the shared installation manifest to `/srv/ecorex-agent-workspace/.ecorex/installations.json`.
- Uses an install lock at `/var/lock/ecorex-web-install.lock` so concurrent online installs on one server are serialized.
- Does not require code signing; it is positioned as the lightweight, high-compatibility Web distribution while still running the full Agent core.

## Build
```powershell
powershell -ExecutionPolicy Bypass -File scripts/prepare-ecorex-web-release.ps1 -Version 0.1.11
```

Output:
- `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz`
- `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz.sha256`

Current artifact:
- Size: `2,845,730`
- SHA256: `2C991A1F5D6EF885C98F25AD5C3502A79D260830A7C106C96E77B53633359828`

The packager is Web-only. It copies runtime source, `channel/web` frontend assets, requirements, install/check scripts, systemd, Caddy, and Nginx examples. `channel/web/static/app` currently contains the desktop renderer static build so `/app/` keeps the desktop visual shell without building or modifying `desktop/`.

## Install
```bash
sudo VERSION=0.1.11 \
  EXPECTED_SHA256=<sha256> \
  bash scripts/install-ecorex-web.sh
```

Local tarball install:
```bash
sudo VERSION=0.1.11 \
  TARBALL_PATH=release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz \
  bash scripts/install-ecorex-web.sh
```

Installer outputs the generated Web password. The same value is stored in `/etc/ecorex-web/ecorex-web.env`.

## Reverse Proxy
Caddy and Nginx examples keep Admin API routes on `127.0.0.1:18084` and proxy Web runtime routes to `127.0.0.1:9909`.

Covered Web paths:
- `/ecorex-agent/app/`
- `/ecorex-agent/auth/*`
- `/ecorex-agent/api/*`
- `/ecorex-agent/message`
- `/ecorex-agent/poll`
- `/ecorex-agent/stream`
- `/ecorex-agent/cancel`
- `/ecorex-agent/chat`
- `/ecorex-agent/assets/*`

SSE routes disable buffering and use long proxy timeouts.

## Verify
Package structure:
```bash
CHECK_INSTALLED=0 CHECK_HTTP=0 bash scripts/check-ecorex-web-release.sh \
  release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz
```

Installed service:
```bash
sudo VERSION=0.1.11 bash scripts/check-ecorex-web-release.sh
```

Public proxy:
```bash
BASE_URL=https://www.ecoreai.cn/ecorex-agent \
WEB_PASSWORD=<password> \
bash scripts/check-ecorex-web-release.sh
```

## Mainline Integration Points
- `deploy/ecorex-site/manifest.json` now records the Web tarball as `ready` with real size and SHA256.
- Upload the tarball to `deploy/ecorex-site/downloads/` or the production download bucket.
- If a standalone Web app build lands outside `desktop/`, pass it with `-WebBuildRoot <path>` so it is copied into `runtime/channel/web/static/app`.
- If `channel/web/static/app/index.html` exists, the packager preserves it and records `webBuild=source-static-app`.


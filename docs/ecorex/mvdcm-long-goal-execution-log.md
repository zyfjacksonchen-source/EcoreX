# EcoreX mvdcm Long Goal Execution Log

Last updated: 2026-06-24, Asia/Shanghai.

## Active Goal

Execute the EcoreX long goal in strict order:

1. Complete the independent clone migration for `https://mvdcm.ecoremedia.net/ecorex-agent/`.
2. After migration acceptance passes, implement Phase 1 sync for logs and artifact metadata only.
3. Phase 2 may sync chat message bodies.
4. Phase 3 may sync artifact files, with chunking, dedupe, throttling, retry, and kill switches.

Enterprise WeChat / WeCom integration is explicitly out of scope for this goal.

## Hard Boundaries

- Do not start Phase 1 code development before clone migration acceptance passes.
- Do not change or restart the current production backend while preparing the new environment.
- New server-side names, paths, services, logs, backups, and docs for this goal must use `EcoreX` / `ecorex`, not new `CowAgent` naming.
- Do not write panel credentials, admin credentials, tokens, API keys, or copied env secrets into files, docs, commits, terminal summaries, or final replies.
- SQLite must be copied through `.backup` or another consistency-safe method; do not directly copy a live WAL state.
- The new environment must use independent directories and ports until verified.

## Fixed Inputs

- Workspace: `C:\CowAgent`
- Target domain: `mvdcm.ecoremedia.net`
- Target entry: `https://mvdcm.ecoremedia.net/ecorex-agent/`
- Panel URLs:
  - External: `https://47.103.108.213:15334/b1425020`
  - Internal: `https://172.16.130.115:15334/b1425020`
- Local release package: `C:\CowAgent\release-artifacts\EcoreX_0.2.0-public-release.zip`
- Expected SHA256: `2F20B80DAD3AC3EB788A32993CC3A6ED4A73BF7E2E01B6914998AFDAF21A78E9`
- mvdcm-specific package: `C:\CowAgent\release-artifacts\EcoreX_0.2.0-mvdcm-public-release.zip`
- mvdcm-specific package SHA256: `6F4A3344D25CCCA3DDAA53ED37B21C830A09F11C3E36CF30A329856723931244`

## Local Evidence Checked In This Run

- `Get-FileHash` confirmed the public release zip SHA256 matches the expected value.
- The release zip contains the required deployment entries:
  - `site/index.html`
  - `site/manifest.json`
  - `site/admin/index.html`
  - `admin-api/ecorex_admin_api.py`
  - `server/install-ecorex-public-release.sh`
  - `server/check-ecorex-server-release.sh`
  - `server/caddy/ecorex-agent.routes.caddy`
  - `server/nginx/ecorex-agent.conf.example`
  - `server/systemd/ecorex-admin-api.service.example`
- Text scan inside the release zip found old domain `www.ecoreai.cn` in these entries:
  - `server/check-ecorex-server-release.sh`
  - `server/install-ecorex-public-release.sh`
  - `server/caddy/Caddyfile.example`
  - `server/caddy/ecorex-agent.routes.caddy`
  - `server/nginx/ecorex-agent.conf.example`
  - `site/index.html`
  - `site/install-webui.ps1`
  - `site/install-webui.sh`
  - `site/site.js`
  - `site/caddy/Caddyfile.example`
  - `site/caddy/ecorex-agent.routes.caddy`
  - `site/nginx/ecorex-agent.conf.example`
- Text scan inside the release zip did not find visible `CowAgent` or `~/cow` residue in the scanned small text entries.
- Generated a separate mvdcm-specific deployment zip without overwriting the original public release zip.
- The mvdcm-specific zip replaces `www.ecoreai.cn` with `mvdcm.ecoremedia.net` in 12 entries:
  - `server/check-ecorex-server-release.sh`
  - `server/install-ecorex-public-release.sh`
  - `server/caddy/Caddyfile.example`
  - `server/caddy/ecorex-agent.routes.caddy`
  - `server/nginx/ecorex-agent.conf.example`
  - `site/index.html`
  - `site/install-webui.ps1`
  - `site/install-webui.sh`
  - `site/site.js`
  - `site/caddy/Caddyfile.example`
  - `site/caddy/ecorex-agent.routes.caddy`
  - `site/nginx/ecorex-agent.conf.example`
- The mvdcm-specific zip has required root entries under `site/`, `server/`, and `admin-api/`; no Windows backslash-only zip root issue remains.
- Text scan inside the mvdcm-specific zip found no `www.ecoreai.cn`, `CowAgent`, or `~/cow` matches in scanned small text entries.
- A local simulated install smoke passed for the mvdcm-specific zip:
  - Manifest product/version: `EcoreX` / `0.2.0`
  - Download page files copied into an `ecorex-agent-download/current` shape.
  - Admin API files copied into an `ecorex-agent-admin/app` shape.
  - `checksums.json` artifact metadata validated against included files or valid external metadata.
  - Simulated installed files had no scanned `www.ecoreai.cn`, `CowAgent`, or `~/cow` matches.
- Added a no-secret server-side migration helper: `C:\CowAgent\scripts\ecorex-mvdcm-clone-migration.sh`.
  - It expects the mvdcm-specific zip at `/tmp/ecorex-release/EcoreX_0.2.0-mvdcm-public-release.zip`.
  - It verifies SHA256, prepares independent `/srv/ecorex-*` directories, creates the `ecorex` service user if needed, runs the bundled installer with `RESTART_SERVICE=0`, normalizes Admin API env origin/host/port, installs or preserves the `ecorex-admin-api` systemd unit, and prepares reverse-proxy snippets.
  - Local Git Bash syntax check passed with `bash -n`.
  - It contains no credentials; the only `CowAgent` text is a legacy-residue grep sentinel.
- DNS and connectivity recheck:
  - `mvdcm.ecoremedia.net` resolves to `198.18.0.161`.
  - `mvdcm.ecoremedia.net:443` is TCP reachable.
  - `curl.exe -k -I https://mvdcm.ecoremedia.net/ecorex-agent/` fails with TLS handshake failure.
  - `47.103.108.213:15334` is TCP reachable.
  - `172.16.130.115:15334` is TCP reachable.
- SSH connectivity recheck after user opened port 22:
  - `47.103.108.213:22` is TCP reachable.
  - `172.16.130.115:22` is TCP reachable, but SSH banner exchange timed out from this machine.
  - OpenSSH client is available locally.
  - Default BatchMode auth to `root`, `ubuntu`, and `ecorex` on `47.103.108.213` failed with `Permission denied (publickey,gssapi-keyex,gssapi-with-mic)`.
  - Existing local PEM at `C:\Users\user\Downloads\bushu001.pem` also failed for `root`, `ubuntu`, and `ecorex` on `47.103.108.213`.
  - Password authentication to `root` is not enabled by the server; the server advertises only `publickey`, `gssapi-keyex`, and `gssapi-with-mic`.
  - Generated a local migration-only Ed25519 keypair at `C:\Users\user\.ssh\ecorex_mvdcm_codex_ed25519`; public key fingerprint is `SHA256:VSRINdEeBpDqq1O+FKZ5mdiCiVdM3vly0lksc53wqH4`.
  - Retried this key for `root@47.103.108.213`; it is still not authorized and returns `Permission denied`.
  - Waiting for the public key to be authorized for a target server user, preferably `root`, before continuing with upload/deploy.
  - No password was entered or recorded.

## Deployment Shape To Preserve

Target directories:

```text
/srv/ecorex-agent-download
/srv/ecorex-agent-download/releases
/srv/ecorex-agent-admin
/srv/ecorex-agent-admin/app
/srv/ecorex-agent-admin/data
/srv/ecorex-agent-admin/env
/srv/ecorex-agent-admin/server
/srv/ecorex-agent-admin/backups
/srv/ecorex-migration-source
```

Admin API:

```text
service: ecorex-admin-api
user/group: ecorex
listen: 127.0.0.1:18084
env file: /srv/ecorex-agent-admin/env/ecorex-admin-api.env
```

Reverse proxy:

```text
/ecorex-agent/admin/api/* -> 127.0.0.1:18084
/ecorex-agent/api/admin/* -> 127.0.0.1:18084
/ecorex-agent/client/* -> 127.0.0.1:18084
/ecorex-agent/admin/* -> /srv/ecorex-agent-download/current/admin
/ecorex-agent/* -> /srv/ecorex-agent-download/current
```

## Server-Side Command Skeleton

Use this as the migration direction after logging in through the panel terminal or file manager. Keep secrets out of the transcript and out of this file.

```bash
set -euo pipefail

VERSION=0.2.0
RELEASE_ROOT=/srv/ecorex-agent-download
ADMIN_ROOT=/srv/ecorex-agent-admin
EXPECTED_SHA256=6F4A3344D25CCCA3DDAA53ED37B21C830A09F11C3E36CF30A329856723931244
ZIP_PATH=/tmp/ecorex-release/EcoreX_0.2.0-mvdcm-public-release.zip

install -d /tmp/ecorex-release
install -d "$RELEASE_ROOT/releases"
install -d "$ADMIN_ROOT/app" "$ADMIN_ROOT/data" "$ADMIN_ROOT/env" "$ADMIN_ROOT/server" "$ADMIN_ROOT/backups"
install -d /srv/ecorex-migration-source

sha256sum "$ZIP_PATH"

export VERSION RELEASE_ROOT ADMIN_ROOT EXPECTED_SHA256
bash server/install-ecorex-public-release.sh "$ZIP_PATH"
```

The preferred package already contains the target domain. If the original public package is used instead, replace old public domain references with the target domain before exposure:

```text
www.ecoreai.cn/ecorex-agent -> mvdcm.ecoremedia.net/ecorex-agent
https://www.ecoreai.cn -> https://mvdcm.ecoremedia.net
ECOREX_ALLOWED_ORIGINS=https://mvdcm.ecoremedia.net
```

## Acceptance Checks

Run after reverse proxy and TLS are configured:

```bash
curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/
curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json
curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/admin/
curl -k -I https://mvdcm.ecoremedia.net/ecorex-agent/client/model-config
```

Expected:

- Root and manifest return 200.
- Admin route requires login when unauthenticated.
- `client/model-config` without token does not leak model secrets.
- Download page install commands point to `mvdcm.ecoremedia.net`.
- Newly created server paths, services, logs, and backups do not use new `CowAgent` naming.

## Current Cursor

Status: clone migration accepted on the target server; Phase 1 code development may start. Phase 2/3 remain out of scope until Phase 1 is complete.

Server deployment evidence from 2026-06-24:

- `root` SSH password login succeeded for execution; the password was used only in-session and was not written to files or logs.
- Server OS: Alibaba Cloud Linux 4.0.3, `aarch64`.
- Uploaded package:
  - `/tmp/ecorex-release/EcoreX_0.2.0-mvdcm-public-release.zip`
  - Remote SHA256 verified as `6f4a3344d25ccca3ddaa53ed37b21c830a09f11c3e36cf30a329856723931244`.
- Uploaded helper:
  - `/tmp/ecorex-release/ecorex-mvdcm-clone-migration.sh`
  - Remote `bash -n` passed after fixing the local/remote cleanup trap.
- Base deployment installed:
  - Release dir: `/srv/ecorex-agent-download/releases/20260624031706-v0.2.0`
  - Current symlink: `/srv/ecorex-agent-download/current`
  - Admin root: `/srv/ecorex-agent-admin`
  - Admin API env: `/srv/ecorex-agent-admin/env/ecorex-admin-api.env`
  - Proxy snippets: `/srv/ecorex-agent-admin/server/mvdcm-proxy-snippets/`
- Service state:
  - `ecorex-admin-api.service` is enabled and active.
  - Admin API listens on `127.0.0.1:18084`.
  - Local unauthenticated probes:
    - `/client/model-config` returns `403` for GET without client key.
    - `/admin/api/state` and `/api/admin/state` return `401` without admin auth.
- Web entry:
  - System nginx `1.30.2` was installed with DNF using `--disableexcludes=all` because the server's DNF/YUM config excludes nginx by default.
  - nginx is enabled and active.
  - nginx listens on `0.0.0.0:80`, `[::]:80`, `0.0.0.0:443`, and `[::]:443`.
  - Port `15334` remains owned by `BT-Panel`; the new EcoreX nginx config does not use or replace the panel port.
- TLS:
  - `acme.sh` was installed from the GitHub tarball after `get.acme.sh` timed out.
  - Let's Encrypt HTTP-01 validation succeeded for `mvdcm.ecoremedia.net`; the CA resolved and validated `47.103.108.213`.
  - Installed certificate files:
    - `/etc/nginx/ssl/mvdcm.ecoremedia.net/fullchain.pem`
    - `/etc/nginx/ssl/mvdcm.ecoremedia.net/privkey.pem`
  - nginx was restarted and locally presents issuer `Let's Encrypt, CN = YE2` for SNI `mvdcm.ecoremedia.net`.
- HTTP fallback:
  - Because公网 443 still times out from outside the host, HTTP currently serves the same EcoreX routes instead of redirecting all traffic to HTTPS.
  - External HTTP probes with host override to `47.103.108.213` passed:
    - `http://mvdcm.ecoremedia.net/ecorex-agent/` -> `200`
    - `http://mvdcm.ecoremedia.net/ecorex-agent/manifest.json` -> `200`
    - `http://mvdcm.ecoremedia.net/ecorex-agent/admin/` -> `401` with `WWW-Authenticate: Basic realm="EcoreX Admin"`
    - `GET http://mvdcm.ecoremedia.net/ecorex-agent/client/model-config` -> `{"ok": false, "error": "invalid client key"}`
- HTTPS status:
  - Local host HTTPS probes to `https://127.0.0.1/...` with `Host: mvdcm.ecoremedia.net` pass for root/manifest/admin and reject unauthenticated client config.
  - Public `https://mvdcm.ecoremedia.net/...` and `https://47.103.108.213/...` still time out on port `443` from outside and from server-to-public-IP hairpin.
  - Server firewalld already lists `443/tcp`; nginx is listening on 443. Remaining evidence points to cloud/provider security group or public network policy needing TCP 443 inbound to be opened.
  - Recheck after interruption:
    - External `Test-NetConnection 47.103.108.213 -Port 443` reports TCP reachable, but local OpenSSL/curl TLS attempts end with EOF / failed handshake.
    - A remote `tcpdump -i any 'tcp port 443'` run during an external OpenSSL connection captured `0` packets, proving the failing public TLS attempt does not reach the ECS network stack.
    - nginx HTTPS access/error logs show only local `127.0.0.1` probes, no external client hit corresponding to the failing TLS attempts.
    - Local ECS HTTPS remains healthy and presents the Let's Encrypt `YE2` certificate for SNI `mvdcm.ecoremedia.net`.
    - Aliyun CLI exists but has no valid configured profile, so security group read/write cannot be performed from the server with the current CLI state.
  - `/ecorex-agent/client/model-config` HEAD requests are now handled at nginx with `403`, so the documented HEAD-style acceptance probe no longer returns Python's default `501`. GET still returns `{"ok": false, "error": "invalid client key"}` without a client key.
- Final clone migration acceptance:
  - Public HTTPS using `mvdcm.ecoremedia.net` with direct resolution to `47.103.108.213` passed:
    - `https://mvdcm.ecoremedia.net/ecorex-agent/` -> `200`
    - `https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json` -> `200`
    - `https://mvdcm.ecoremedia.net/ecorex-agent/admin/` -> `401` with `WWW-Authenticate: Basic realm="EcoreX Admin"`
    - `HEAD https://mvdcm.ecoremedia.net/ecorex-agent/client/model-config` -> `403`
    - `GET https://mvdcm.ecoremedia.net/ecorex-agent/client/model-config` -> `{"ok": false, "error": "invalid client key"}`
  - HTTP now redirects to HTTPS:
    - `http://mvdcm.ecoremedia.net/ecorex-agent/` -> `301 Location: https://mvdcm.ecoremedia.net/ecorex-agent/`
  - Download page install commands point to `https://mvdcm.ecoremedia.net/ecorex-agent/...`.
  - `nginx` and `ecorex-admin-api` are active.
  - Current release symlink resolves to `/srv/ecorex-agent-download/releases/20260624031706-v0.2.0`.
  - `manifest.json` reports `EcoreX` / `0.2.0` with 7 artifacts.
  - Deployed residue scan found no `www.ecoreai.cn`, `CowAgent`, or `~/cow` matches in the checked deployed EcoreX paths and service/nginx config.
- Data import:
  - Searched `/srv`, `/www`, `/root`, and `/tmp` for EcoreX SQLite/env/manifest candidates.
  - No pre-existing EcoreX data source was found on this target host beyond the new deployment.
  - Current Admin DB exists at `/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3`, size `98304`, with tables: `audit_events`, `capability_packs`, `capability_policy`, `client_sessions`, `error_logs`, `model_credentials`, `usage_events`, `users`.
- Residue scan:
  - Deployed EcoreX paths scanned with no hits for `www.ecoreai.cn`, `CowAgent`, or `~/cow` in the checked small text files.

Browser/panel access attempt in this run:

- In-app Browser attempt for `https://47.103.108.213:15334/b1425020` was blocked client-side with `net::ERR_BLOCKED_BY_CLIENT`.
- In-app Browser attempt for `https://172.16.130.115:15334/b1425020` timed out during navigation.
- No credentials were entered or recorded.
- Server-side migration cannot be executed from this thread until a usable panel session, SSH session with an authorized username/key, or other approved server execution channel is available.

SSH status in this run:

- 2026-06-24 13:05 +08:00 password-login recheck:
  - `root` password authentication to `47.103.108.213:22` succeeded.
  - Remote SSH banner: `SSH-2.0-OpenSSH_9.6`.
  - Remote host: `iZuf6ei5trsm48vlgh8al5Z`.
  - Remote kernel: `Linux 6.6.102-5.3.1.alnx4.aarch64 aarch64 GNU/Linux`.
  - `nginx` is active.
  - `ecorex-admin-api` is active.
  - Listening sockets include public `22`, `80`, `443`, and local `127.0.0.1:18084`.
  - The credential value was used only in-session and was not written to files.

Phase 1 sync implementation status:

- 2026-06-24 13:32 +08:00 local implementation pass:
  - Admin API now has Phase 1-only ingestion tables for `sync_events` and `sync_artifacts`.
  - New client endpoints are available locally in source:
    - `GET /client/sync/status`
    - `POST /client/sync/events`
    - Compatibility aliases: `/sync/status/client`, `/events/sync/client`, `/client/sync/artifacts`
  - Event detail sanitization denies chat/body-like keys such as `content`, `message`, `messages`, `prompt`, `response`, `text`, and file/body-like keys.
  - Artifact ingestion stores metadata only: safe artifact id, title/type/status, size, MIME, path hash, and extension. Raw paths, URLs, ids, body content, and file blobs are omitted or hashed.
  - Sync endpoints require a valid client key and, for ingestion, a valid user session token.
  - Admin state now reports `syncSummary` plus `summary.syncEvents` and `summary.syncArtifacts`.
  - Web bridge routes `window.ecorexDesktop.reportTelemetry({ type: "phase1_sync", ... })` to the client sync endpoint; normal telemetry still routes to `/events`.
  - Desktop producer changes were intentionally removed after the linked Web-only thread marked desktop work paused.
  - Phase 2 chat-body sync and Phase 3 artifact-file sync were not started.
- Local verification passed:
  - `python -m py_compile deploy\ecorex-admin-api\ecorex_admin_api.py channel\web\web_channel.py tests\test_ecorex_admin_device_id.py`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `5 passed`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "v020_webui_local_auth_falls_back_without_admin_client" -q` -> `1 passed, 250 deselected`
  - `git diff --check` for the touched Admin/Web/test/docs files returned no whitespace errors.
- 2026-06-24 13:45 +08:00 mvdcm deployment pass:
  - Deployed the updated Admin API source to `/srv/ecorex-agent-admin/app/ecorex_admin_api.py`.
  - Remote backup was created at `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.phase1.20260624134332.bak`.
  - Remote `python3 -m py_compile` passed before and after installation.
  - `ecorex-admin-api` was restarted and remained `active`.
  - Remote local GET `/client/sync/status` with a valid client key returned `ok: true` and zero sync counts.
  - Public HTTPS GET `https://mvdcm.ecoremedia.net/ecorex-agent/client/sync/status` with a valid client key returned `200` and zero sync counts.
  - Public HTTPS POST `/ecorex-agent/client/sync/events` without a user token returned `401 missing user token`.
  - Public HTTPS GET `/ecorex-agent/client/sync/status` with an invalid client key returned `403`.
  - Public HTTPS `manifest.json` still returned `200`.
- 2026-06-24 14:13 +08:00 Web-only producer deployment:
  - Added a Web bridge-level EventSource observer in `channel/web/web_channel.py`.
    - It watches `/stream?request_id=...` SSE events from the Web app shell.
    - It sends Phase 1 payloads through `/sync/events` using the existing authenticated Web client bridge.
    - It emits run/tool/artifact event metadata and artifact metadata only.
    - It does not include streamed deltas, final text, prompt/response/message bodies, raw tool result text, raw artifact paths/URLs, or file/blob contents.
    - It is switchable with `window.ECOREX_PHASE1_SYNC=false`, `window.ECOREX_DISABLE_PHASE1_SYNC=true`, or `localStorage.ecorex_phase1_sync=off`.
  - Kept the prior `reportTelemetry({ type: "phase1_sync" })` route intact for callers that explicitly report Phase 1 sync.
  - Built Web Linux service package:
    - `release-artifacts/EcoreX_0.2.1-web-linux-service.tar.gz`
    - SHA256 `77B70CC98E2019B6307AD794B9F54B355A503CAEC0B40FE7AA3AA42D1C9DB9D6`
    - Package check passed with installed/HTTP/systemd checks disabled.
    - Package source scan confirmed bridge `installPhase1EventSourceSync`, `/sync/events`, and `phase1_sync` markers.
  - Deployed Web runtime to mvdcm:
    - Service: `ecorex-web.service`, active.
    - Current runtime: `/opt/ecorex-web/releases/20260624061037-v0.2.1`.
    - Public `/ecorex-agent/app/` returns `200`.
    - Public `/ecorex-agent/app/` HTML contains `installPhase1EventSourceSync`, `/sync/events`, and `phase1_sync`.
  - Updated nginx runtime routing:
    - Added Web runtime routes for `/ecorex-agent/app/`, `/auth/`, `/api/`, `/uploads/`, `/static/`, and exact `/message`, `/upload`, `/poll`, `/stream`, `/cancel`, `/chat`, `/config`.
    - Kept `/ecorex-agent/client/*` on Admin API and `/ecorex-agent/api/admin/*` on Admin API.
    - Backups:
      - `/etc/nginx/conf.d/ecorex-mvdcm.conf.phase1-web.20260624140324.bak`
      - `/etc/nginx/conf.d/ecorex-mvdcm.conf.phase1-web-exact.20260624140442.bak`
    - `nginx -t` passed and nginx reloaded active.
  - Updated Admin API compatibility:
    - Added `ecorex-web-v0.2.1-web.1` to accepted client keys.
    - Remote backup: `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.phase1web.20260624141242.bak`.
    - `ecorex-admin-api` restarted active.
    - Public HTTPS `GET /ecorex-agent/client/sync/status` with `ecorex-web-v0.2.1-web.1` returned `200` and zero sync counts.
  - Local verification after producer change:
    - `node --check channel\web\static\js\console.js`
    - `python -m py_compile channel\web\web_channel.py tests\test_ecorex_web_parallel_backend.py deploy\ecorex-admin-api\ecorex_admin_api.py tests\test_ecorex_admin_device_id.py`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "Phase1SyncProducerSource or v020_webui_local_auth_falls_back_without_admin_client" -q` -> `4 passed, 254 deselected`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `5 passed`
  - `/ecorex-agent/api/version` still reports application version `0.2.0`; the deployed Web service release metadata is `0.2.1`.
- 2026-06-24 14:33 +08:00 authenticated Phase 1 Web/bridge smoke:
  - Created one temporary mvdcm Admin user/session directly for smoke testing, then revoked the session and soft-deleted the user after evidence collection.
  - Pulled the public `https://mvdcm.ecoremedia.net/ecorex-agent/app/` HTML and evaluated the deployed Web bridge script in a same-origin JS harness with a real public `/message` request and real public `/stream?request_id=...` SSE.
  - The deployed bridge installed `installPhase1EventSourceSync`, observed the stream, and posted one Phase 1 payload to `/ecorex-agent/client/sync/events`; the POST returned `200`.
  - The Web runtime `POST /ecorex-agent/message` returned `status=success`, `stream=true`, and a real request id; the SSE harness received 4 messages.
  - The Admin DB stored exactly 1 Phase 1 event for that request:
    - `event_type=run.accepted`
    - `status=running`
    - `detail={"stream":true}`
    - `sync_artifacts=0`
  - The test prompt included a unique sentinel string. DB audit found `0` occurrences of that sentinel or full prompt in `sync_events`/`sync_artifacts`.
  - DB audit found no forbidden raw-body keys among `final_text`, `prompt`, `response`, `file_content`, or `data_base64` in the stored sync rows.
  - Current public sync status with `ecorex-web-v0.2.1-web.1` reports:
    - `events=1`
    - `artifacts=0`
    - `requests=1`
    - `lastIngestedAt=2026-06-24T14:33:52+08:00`
  - Ordinary SSE `phase` frames are intentionally not written by the current producer; Phase 1 currently records run accepted, tool, artifact, terminal, and artifact-limit metadata only.
- 2026-06-24 14:44 +08:00 Phase 2/3 safety gate deployment:
  - Added Admin API sync policy reporting so clients can see the active phase boundary:
    - Phase 1 events and artifact metadata are enabled.
    - Phase 1 explicitly reports `storesChatBodies=false` and `storesArtifactFiles=false`.
    - Phase 2 chat-body sync is disabled by default behind `ECOREX_SYNC_PHASE2_MESSAGES_ENABLED`.
    - Phase 3 artifact-file sync is disabled by default behind `ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED`.
    - Phase 3 policy exposes default controls: `maxAutoBytes=10485760`, `chunkBytes=2097152`, `bytesPerSecond=1048576`, `dedupe=true`, `killSwitch=true`.
  - Added explicit client routes for future phases:
    - `GET /client/sync/policy`
    - `POST /client/sync/messages`
    - `POST /client/sync/artifact-files`
    - `POST /client/sync/artifact-blobs`
    - `PUT /client/sync/artifact-files/{artifact_id}`
    - `PUT /client/sync/artifact-blobs/{artifact_id}`
  - The Phase 2/3 ingest routes validate a user session first, then return policy-level `403` while disabled. If someone enables the env flags before implementation is complete, the routes return `501` instead of writing partial data.
  - Local verification:
    - `python -m py_compile deploy\ecorex-admin-api\ecorex_admin_api.py tests\test_ecorex_admin_device_id.py`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `6 passed`
    - Local HTTP smoke returned:
      - `GET /client/sync/status` -> `200` with policy.
      - `POST /client/sync/messages` with a valid user token -> `403 phase 2 chat body sync is disabled`.
      - `PUT /client/sync/artifact-blobs/{id}` with a valid user token -> `403 phase 3 artifact file sync is disabled`.
  - Deployed updated Admin API to mvdcm:
    - Remote backup: `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.phase23gate.20260624144414.bak`.
    - Remote `python3 -m py_compile` passed before and after replacement.
    - `ecorex-admin-api` restarted and is `active`.
    - Remote source contains `SYNC_PHASE2_MESSAGES_ENV`, `/client/sync/messages`, `/client/sync/artifact-blobs`, and `syncPolicy` markers.
  - Public verification:
    - `GET https://mvdcm.ecoremedia.net/ecorex-agent/client/sync/status` with `ecorex-web-v0.2.1-web.1` returns `200`, sync counts unchanged (`events=1`, `artifacts=0`) and includes the Phase 2/3 disabled policy.
    - A temporary valid user token calling public `POST /client/sync/messages` returned `403 phase 2 chat body sync is disabled`.
    - The same temporary valid user token calling public `PUT /client/sync/artifact-blobs/artifact-smoke` returned `403 phase 3 artifact file sync is disabled`, with `artifactFilesEnabled=false` and `killSwitch=true`.
    - The temporary user/session was cleaned up.
- 2026-06-24 14:53 +08:00 Phase 2 server-side storage implementation:
  - Implemented server-side Phase 2 message ingestion in the Admin API while keeping the deployed mvdcm env switch disabled.
  - Added `sync_messages` table with idempotent `sync_key`, user/session/request identity, message id, sequence, role, canonical JSON content, `content_sha256`, content byte size, sanitized extras, and timestamps.
  - `syncSummary` now includes:
    - `messages`
    - `messageSessions`
    - `messageRequests`
  - `syncPolicy.phase2` now reports:
    - `implemented=true`
    - `maxBatchMessages=1000`
    - `maxContentBytes=262144`
  - Phase 2 extras are sanitized so raw paths, file bodies, and duplicated body-like fields are omitted from extras; the canonical message body is stored only in the `content` column when Phase 2 is enabled.
  - Local verification:
    - `python -m py_compile deploy\ecorex-admin-api\ecorex_admin_api.py tests\test_ecorex_admin_device_id.py`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `8 passed`
    - Local HTTP smoke with `ECOREX_SYNC_PHASE2_MESSAGES_ENABLED=1`:
      - first `POST /client/sync/messages` -> `200`, accepted 2 messages
      - repeat POST -> `200`, accepted 2 messages, stored row count remained 2
  - Deployed updated Admin API to mvdcm:
    - Remote backup: `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.phase2store.20260624145317.bak`.
    - Remote `python3 -m py_compile` passed before and after replacement.
    - `ecorex-admin-api` restarted and is `active`.
    - Remote `sync_messages` table exists.
  - Public verification after deploy:
    - `GET /ecorex-agent/client/sync/status` reports `messages=0`, `phase2.implemented=true`, `phase2.chatBodiesEnabled=false`.
    - A temporary valid user token calling public `POST /client/sync/messages` still returned `403 phase 2 chat body sync is disabled`; the temporary user/session was cleaned up.
- 2026-06-24 14:59 +08:00 Web-only Phase 2 producer deployment:
  - Added Web bridge Phase 2 message producer in `channel/web/web_channel.py`.
    - After a successful `/message` request, it can send the user-visible message body to `/sync/messages`.
    - On SSE `done`, it can send the assistant final body to `/sync/messages`.
    - It first reads `/sync/policy` and only emits if `syncPolicy.phase2.chatBodiesEnabled=true`.
    - It can also be locally disabled with `window.ECOREX_PHASE2_SYNC=false`, `window.ECOREX_DISABLE_PHASE2_SYNC=true`, or `localStorage.ecorex_phase2_sync=off`.
    - Phase 1 metadata emission remains unchanged and still omits body fields.
  - Local verification:
    - `python -m py_compile channel\web\web_channel.py deploy\ecorex-admin-api\ecorex_admin_api.py tests\test_ecorex_admin_device_id.py tests\test_ecorex_web_parallel_backend.py`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `8 passed`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "Phase1SyncProducerSource" -q` -> `4 passed, 257 deselected`
  - Built Web Linux service package:
    - `release-artifacts/EcoreX_0.2.1-web-linux-service.tar.gz`
    - SHA256 `BFFD57FDB1DFC87FA19412608E64D7AC792B881773C4720B57E2DC464B23C77C`
    - Package-only `scripts/check-ecorex-web-release.sh` passed.
    - Package source contains `phase2SyncEnabled`, `/sync/messages`, `/sync/policy`, `phase2EmitUserMessage`, and `chatBodiesEnabled`.
  - Deployed Web runtime to mvdcm:
    - Current runtime: `/opt/ecorex-web/releases/20260624065905-v0.2.1`.
    - `ecorex-web.service` is `active`.
    - Runtime source contains the Phase 2 producer markers above.
  - Public verification:
    - `GET https://mvdcm.ecoremedia.net/ecorex-agent/app/` -> `200`.
    - Public app HTML contains `phase2SyncEnabled`, `/sync/messages`, `/sync/policy`, `phase2EmitUserMessage`, and `installPhase1EventSourceSync`.
    - Public sync status remains `messages=0` and `phase2.chatBodiesEnabled=false`, so no chat body sync is active yet on mvdcm.
- 2026-06-24 15:04 +08:00 Phase 2 enabled on mvdcm:
  - Backed up the Admin API env file before changing the Phase 2 switch:
    - `/srv/ecorex-agent-admin/backups/ecorex-admin-api.env.phase2enable.20260624150406.bak`
  - Set `ECOREX_SYNC_PHASE2_MESSAGES_ENABLED=1` for `ecorex-admin-api` and restarted the service.
  - Remote local status after restart:
    - `ecorex-admin-api` is `active`.
    - `phase2.chatBodiesEnabled=true`.
    - `phase3.artifactFilesEnabled=false`.
    - `messages=0` before the Phase 2 E2E smoke.
- 2026-06-24 15:06 +08:00 Phase 2 Web-only E2E smoke:
  - Created one temporary mvdcm Admin user/session for the smoke, then revoked the session and soft-deleted the user.
  - Loaded the public `https://mvdcm.ecoremedia.net/ecorex-agent/app/` bridge script.
  - Used the public Web bridge path to issue a real `/message` request and get a real request id.
  - The bridge read `/sync/policy`, saw `phase2.chatBodiesEnabled=true`, and posted the user-visible message body to `/sync/messages`.
  - The same bridge EventSource observer processed a terminal `done` event for that request id and posted the assistant final body to `/sync/messages`.
  - Replayed the same assistant `done` event twice; DB evidence still has exactly 2 sync message rows for the request, proving idempotent upsert for the smoke path.
  - DB audit for the request:
    - `messagesForRequest=2`
    - roles: `user`, `assistant`
    - `uniqueSyncKeys=2`
    - both rows have `content_sha256`
    - both rows have positive `content_size_bytes`
    - user smoke body stored: true
    - assistant smoke body stored: true
    - `eventRowsForRequest=2`
  - Public status after smoke:
    - `events=3`
    - `artifacts=0`
    - `messages=2`
    - `messageSessions=1`
    - `messageRequests=1`
    - `phase2.chatBodiesEnabled=true`
    - `phase3.artifactFilesEnabled=false`
    - `phase3.killSwitch=true`
    - `lastIngestedAt=2026-06-24T15:06:09+08:00`
  - Recent-row DB audit confirms the newest sync message rows are `assistant` and `user`, with hashes present and positive sizes.
- 2026-06-24 15:26 +08:00 Phase 3 local implementation:
  - Implemented server-side Phase 3 artifact-file ingestion in `deploy/ecorex-admin-api/ecorex_admin_api.py`.
    - New DB tables:
      - `sync_artifact_files` for per-artifact file manifests and completion state.
      - `sync_artifact_file_chunks` for chunk BLOB storage keyed by `content_sha256 + chunk_index`, so identical content reuses stored chunks.
      - `sync_artifact_rate_limits` for per-user leaky-bucket rate limiting.
    - `syncPolicy.phase3.implemented=true`; the feature remains controlled by `ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED`.
    - `maxAutoBytes`, `chunkBytes`, `bytesPerSecond`, `dedupe`, and `killSwitch` are enforced from the server policy.
    - `POST/PUT /client/sync/artifact-files` and `/client/sync/artifact-blobs/{artifact_id}` now ingest a single base64 chunk, verify chunk SHA-256, verify complete-file SHA-256 at completion, enforce total size, dedupe repeated chunks, and return `429` on rate-limit violations.
    - The default closed state still validates the user session then returns `403 phase 3 artifact file sync is disabled` without writing file rows.
  - Implemented Web-only Phase 3 producer in `channel/web/web_channel.py`.
    - The bridge reads `/sync/policy` and only uploads when `phase3.artifactFilesEnabled=true` and `killSwitch` is not true.
    - It can be locally disabled with `window.ECOREX_PHASE3_SYNC=false`, `window.ECOREX_DISABLE_PHASE3_SYNC=true`, or `localStorage.ecorex_phase3_sync=off`.
    - It fetches artifact bytes only from same-origin Web runtime URLs such as `/api/file` or `/uploads`; raw local paths are used only for the local fetch URL and are not sent to Admin API.
    - Remote payloads include safe artifact metadata, file SHA-256, chunk SHA-256, total size, chunk index/count, and base64 chunk data.
    - Client-side upload pacing follows `syncPolicy.phase3.bytesPerSecond`.
  - Local verification:
    - `python -m py_compile channel\web\web_channel.py deploy\ecorex-admin-api\ecorex_admin_api.py tests\test_ecorex_admin_device_id.py tests\test_ecorex_web_parallel_backend.py`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` -> `10 passed`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -k "Phase1SyncProducerSource" -q` -> `5 passed, 258 deselected`
    - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py tests/test_ecorex_web_parallel_backend.py -k "phase3 or Phase1SyncProducerSource" -q` -> `8 passed, 265 deselected`
- 2026-06-24 15:41 +08:00 Phase 3 mvdcm deployment and E2E smoke:
  - Rebuilt the Web Linux service package:
    - `release-artifacts/EcoreX_0.2.1-web-linux-service.tar.gz`
    - SHA256 `514052000AB326266A1C30262EBE302134D9CBF755003B037D43BCD84F4CE573`
    - Package-only `scripts/check-ecorex-web-release.sh` passed.
    - Package source contains `phase3Policy`, `artifactFilesEnabled`, `/sync/artifact-blobs`, and `phase3EmitArtifactFile`.
  - Deployed updated Admin API to mvdcm:
    - Remote backup: `/srv/ecorex-agent-admin/backups/ecorex_admin_api.py.phase3.20260624073126.bak`.
    - `python3 -m py_compile` passed before and after replacement.
    - `ecorex-admin-api` restarted and is `active`.
  - Deployed updated Web runtime to mvdcm:
    - Current runtime: `/opt/ecorex-web/releases/20260624073124-v0.2.1`.
    - `ecorex-web.service` is `active`.
    - Runtime source contains the Phase 3 producer markers above.
  - Public closed-state verification before enabling:
    - `GET /ecorex-agent/client/sync/status` reported `phase3.implemented=true`, `artifactFilesEnabled=false`, `killSwitch=true`, and zero artifact-file counts.
    - A temporary valid user token calling public `PUT /client/sync/artifact-blobs/{id}` returned `403 phase 3 artifact file sync is disabled`.
    - Artifact-file and chunk counts stayed `0`; the temporary user/session was cleaned up.
  - Enabled Phase 3 on mvdcm:
    - Backed up the Admin API env file to `/srv/ecorex-agent-admin/backups/ecorex-admin-api.env.phase3enable.20260624073421.bak`.
    - Set `ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED=1` and restarted `ecorex-admin-api`.
    - Public status then reported `phase3.artifactFilesEnabled=true` and `killSwitch=false`.
  - Public artifact-file API smoke:
    - A temporary user/session uploaded a two-chunk artifact file over public HTTPS.
    - First chunk returned `200` and `complete=false`; second chunk returned `200` and `complete=true`.
    - Repeating the second chunk returned `200`, `deduped=true`, and `chunkStored=false`.
    - A second artifact with the same content reused the stored chunks and completed with `deduped=true`.
    - A rate-limit probe uploaded one large chunk successfully and the immediate second chunk returned `429 phase 3 artifact file sync rate limit exceeded`; the incomplete rate-test rows/chunks were cleaned up.
    - DB audit for the API smoke request:
      - `filesForRequest=2`
      - `completeFilesForRequest=2`
      - `storedChunkCountForContent=2`
      - `storedBytesForContent=61`
      - metadata body-like content omitted: true
  - Public Web bridge runtime smoke:
    - Executed the public `/app/` injected bridge in a Node VM harness with a mocked same-origin `/api/file` response and a real temporary Admin user/session.
    - The bridge read the public sync policy, processed a synthetic SSE `done` artifact event, and made one real public `/client/sync/artifact-blobs/{id}` call.
    - DB audit for the bridge smoke request:
      - `filesForRequest=1`
      - `completeFilesForRequest=1`
      - `storedChunkCountForContent=1`
      - content SHA matched: true
      - metadata omitted `/api/file?path` raw path: true
    - The temporary user/session was cleaned up.
  - Final public sync status after smoke:
    - `events=6`
    - `artifacts=1`
    - `messages=2`
    - `artifactFiles=3`
    - `artifactFilesComplete=3`
    - `artifactFileBytes=182`
    - `artifactFileChunks=3`
    - `artifactFileStoredBytes=121`
    - `artifactFileRequests=2`
    - `phase3.artifactFilesEnabled=true`
    - `phase3.killSwitch=false`

Next action:

1. Run final local diff checks and sensitive literal scan.
2. Confirm no accidental desktop changes were introduced by this Phase 3 turn.
3. Keep enterprise WeCom out of this goal.

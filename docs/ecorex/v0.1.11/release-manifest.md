# EcoreX v0.1.11 Release Manifest

## Local Artifacts
- Windows installer: `desktop/release/EcoreX_0.1.11_x64-setup.exe`
- Current rebuilt size: `117,572,805`
- Current rebuilt SHA256: `CF0C5FFAFDF8A0C7FC0991BDFCBE5609917375D232AA419AAFCAE6329321CD18`
- Current signature: Authenticode `NotSigned`
- Previous signed artifact: size `117,469,216`, SHA256 `5ADF10F90DB64E46C6A92CB9FC0730F0A37D0C45B2F55B7EC566E25CF12E3685`
- WebUI Linux service: `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz`
- WebUI size: `2,846,017`
- WebUI SHA256: `7C08D86502943275E40E1924D6283D5419C2A2BF769078EB9AABC9B3E3AE9FC2`
- macOS arm64 DMG: `desktop/release/EcoreX_0.1.11_arm64.dmg`, size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`, status `ready-unsigned`
- macOS x64 DMG: `desktop/release/EcoreX_0.1.11_x64.dmg`, size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`, status `ready-unsigned`
- Public release zip: `release-artifacts/EcoreX_0.1.11-public-release.zip`
- Public release zip size: `311,225,935`
- Public release zip SHA256: `71D2196AEEF4A331F321D996839E09D8D9A03B70DB113C959B4048F48B6C9DE7`
- Git handoff bundle: `release-artifacts/EcoreX_0.1.11-productization.bundle`
- Git handoff bundle: regenerate from final `HEAD` after release-note commits, then verify with `git bundle verify` and `Get-FileHash`.
- GitHub source snapshot: `main` and `codex/ecorex-v0.1.11-productization` both point to API snapshot commit `71c5578a889708aa8f652116084af055de61f2d9`, created from local source commit `692aa5ea83b43b871d955395d47e3badd4d63320`.

## Public Manifest State
- Product: `EcoreX`
- Version: `0.1.11`
- Windows: rebuilt locally and recorded in manifest as `pending-signature`; do not publish until Authenticode signing is completed.
- WebUI Linux service: ready locally, published publicly, and installed on production as the parallel `0.1.11-web.1` lightweight Web distribution.
- Public release zip: generated with the current ready Web artifact and both macOS `ready-unsigned` DMGs; Windows remains `pending-signature`.
- macOS arm64 DMG: real artifact recorded in manifest as `ready-unsigned`.
- macOS x64 DMG: real artifact recorded in manifest as `ready-unsigned`.

## Build Notes
- Standard `npm run package:win:signed` rebuilt runtime and renderer but hit an Electron Builder network timeout during full directory packaging.
- Electron Builder then hit a Windows `EPERM` rename while extracting Electron. The local builder dependency was patched to retry rename and fall back to copying the extracted directory. This is a build-host workaround only, not product runtime code.
- The latest unpacked package was generated from clean staged runtime and passed the no-local-state scan.
- NSIS generation passed after reusing the complete cached NSIS toolset.
- Unsigned installed-app smoke passed for the current rebuilt installer: silent install, app launch, sidecar ready, and `/api/tools` returned `bash`, `web_fetch`, and `browser`.
- Authenticode signing is currently blocked by the cloud-signing/private-key provider interaction. The certificate is visible in CurrentUser/My and matches the previous signed installer, but both `signtool` and `Set-AuthenticodeSignature` hang or fail while accessing the private key. Do not publish the current rebuilt installer as final until it is signed.
- More precise signing diagnostic: Windows SDK `signtool /debug` sees the certificate and leaves one candidate after hash filtering, but zero candidates after the private-key filter. `SimplySign CSP`/`SimplySign KSP` are installed, while `certutil -key` shows no active key container. `SCardSvr` and `ScDeviceEnum` are stopped and need an elevated/UAC-approved start before SimplySign can expose the signing key.

## Clean Release Boundary
- The packaged app must include source/runtime templates only.
- Do not package `%APPDATA%/ecorex-desktop`, `%APPDATA%/ecorex-agent`, `~/cow`, local session history, local logs, pasted-files cache, or generated developer artifacts.
- Before upload, scan `release/win-unpacked/resources` and final installers/DMGs for session/history/log/cache/database files that are not source-controlled runtime assets.

## Server Notes
- Production Admin API source and Docker build context were updated to v0.1.11.
- Docker Compose service `ecorex-admin-api` was rebuilt and is healthy.
- `ECOREX_CLIENT_EVENT_KEYS` now includes `ecorex-desktop-v0.1.10`, `ecorex-desktop-v0.1.11`, and `ecorex-web-v0.1.11-web.1`.
- Capability policy default is now `preinstall`; production DB was migrated to `mode = preinstall` and public `/client/capability-policy` returns version `0.1.11`.
- WebUI service installs independently as `ecorex-web`, defaults to `127.0.0.1:9909`, serializes concurrent installs with `/var/lock/ecorex-web-install.lock`, and stores shared cross-surface state under `<agent_workspace>/.ecorex/`.
- Production Docker Caddy reaches the host WebUI service through bridge `172.18.0.1:9909`; `/etc/ecorex-web/ecorex-web.env` has `WEB_HOST=172.18.0.1`, while public access remains `https://www.ecoreai.cn/ecorex-agent/app/`.
- WebUI `/app/` now packages `channel/web/static/app`, copied from the existing desktop renderer static output without running desktop build steps during this Web release pass.
- The public release zip was structure-validated locally: static site, Admin API, server helpers, strict JSON `checksums.json`, `site/downloads/EcoreX_0.1.11-web-linux-service.tar.gz`, `site/downloads/EcoreX_0.1.11_arm64.dmg`, and `site/downloads/EcoreX_0.1.11_x64.dmg` are present; all staged artifact hashes/sizes match `checksums.json`.
- Production `install-ecorex-public-release.sh` deployed `/srv/ecorex-agent-download/releases/20260612115714-v0.1.11`; `check-ecorex-server-release.sh` passed local artifact validation and public HTTP checks, including manifest payload version/status validation.
- Production `install-ecorex-web.sh` deployed `/opt/ecorex-web/releases/20260612115734-v0.1.11`; `check-ecorex-web-release.sh` passed both host-side service checks and public proxy checks for login, `/app/`, `/auth/check`, `/api/version`, and SSE.
- Direct `git push ecorex HEAD:codex/ecorex-v0.1.11-productization` is blocked on this machine by GitHub SSH `Permission denied (publickey)`. GitHub Git Data API was used instead to update `main` and `codex/ecorex-v0.1.11-productization` to source snapshot `71c5578a889708aa8f652116084af055de61f2d9`.

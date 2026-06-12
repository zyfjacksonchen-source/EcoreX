# EcoreX v0.1.11 Release Manifest

## Local Artifacts
- Windows installer: `desktop/release/EcoreX_0.1.11_x64-setup.exe`
- Current rebuilt size: `117,428,366`
- Current rebuilt SHA256: `0E18A6FC935EA37D93452B238BD3A313673BF3970B2BE7AF381ABB3AA4F06851`
- Current signature: Authenticode `NotSigned`
- Previous signed artifact: size `117,469,216`, SHA256 `5ADF10F90DB64E46C6A92CB9FC0730F0A37D0C45B2F55B7EC566E25CF12E3685`
- WebUI Linux service: `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz`
- WebUI size: `2,845,730`
- WebUI SHA256: `2C991A1F5D6EF885C98F25AD5C3502A79D260830A7C106C96E77B53633359828`
- macOS arm64 DMG: `desktop/release/EcoreX_0.1.11_arm64.dmg`, size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`, status `ready-unsigned`
- macOS x64 DMG: `desktop/release/EcoreX_0.1.11_x64.dmg`, size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`, status `ready-unsigned`
- Public release zip: `release-artifacts/EcoreX_0.1.11-public-release.zip`
- Public release zip size: `5,648,452`
- Public release zip SHA256: `5826F726869ABC9907CC243800E1F4A2372DE6AB77B5A948CD4CFBC9443B1256`

## Public Manifest State
- Product: `EcoreX`
- Version: `0.1.11`
- Windows: rebuilt locally and recorded in manifest as `pending-signature`; do not publish until Authenticode signing is completed.
- WebUI Linux service: ready locally and recorded in manifest as the parallel `0.1.11-web.1` lightweight Web distribution.
- Public release zip: generated with the current ready Web artifact; Windows remains `pending-signature` and macOS remains pending.
- macOS arm64 DMG: real artifact recorded in manifest as `ready-unsigned`.
- macOS x64 DMG: real artifact recorded in manifest as `ready-unsigned`.

## Build Notes
- Standard `npm run package:win:signed` rebuilt runtime and renderer but hit an Electron Builder network timeout during full directory packaging.
- Electron Builder then hit a Windows `EPERM` rename while extracting Electron. The local builder dependency was patched to retry rename and fall back to copying the extracted directory. This is a build-host workaround only, not product runtime code.
- The latest unpacked package was generated from clean staged runtime and passed the no-local-state scan.
- NSIS generation passed after reusing the complete cached NSIS toolset.
- Unsigned installed-app smoke passed for the current rebuilt installer: silent install, app launch, sidecar ready, and `/api/tools` returned `bash`, `web_fetch`, and `browser`.
- Authenticode signing is currently blocked by the cloud-signing/private-key provider interaction. The certificate is visible in CurrentUser/My and matches the previous signed installer, but both `signtool` and `Set-AuthenticodeSignature` hang or fail while accessing the private key. Do not publish the current rebuilt installer as final until it is signed.

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
- WebUI `/app/` now packages `channel/web/static/app`, copied from the existing desktop renderer static output without running desktop build steps during this Web release pass.
- The public release zip was structure-validated locally: static site, Admin API, server helpers, `checksums.json`, and `site/downloads/EcoreX_0.1.11-web-linux-service.tar.gz` are present and the Web artifact hash/size matches `checksums.json`.

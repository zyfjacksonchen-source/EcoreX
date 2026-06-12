# EcoreX v0.1.11 Notes

## Scope
- Desktop UI/UX correction pass for the Codex-style two-column shell.
- WebUI-compatible SSE restoration for streaming reasoning, tool calls, media, cancellation, and completion events.
- Long assistant replies collapse by default and can be expanded inline.
- Factory persona defaults to professional, rigorous, restrained; address the user as "同学"; external identity is always EcoreX.
- Admin API and desktop enterprise policy move to the v0.1.11 client channel while retaining v0.1.10 compatibility.
- Parallel `0.1.11-web.1` WebUI Linux service package ships as a no-signature lightweight distribution while running the full Agent core.

## Current Evidence
- Windows installer: `desktop/release/EcoreX_0.1.11_x64-setup.exe`
- Current rebuilt Windows installer size: `117,428,366` bytes
- Current rebuilt Windows installer SHA256: `0E18A6FC935EA37D93452B238BD3A313673BF3970B2BE7AF381ABB3AA4F06851`
- Authenticode: `NotSigned` for the current rebuilt installer; previous signed installer was superseded by later fixes.
- Installed Windows smoke: passed unsigned; app started, sidecar ready, `/auth/check` returned success, and `/api/tools` included `bash`, `web_fetch`, and `browser`.
- Renderer visual smoke: passed for auth, main, settings, abilities, light and dark modes.
- Production Admin API hotfix: deployed v0.1.11 code and confirmed `ecorex-desktop-v0.1.11` reaches the public capability-policy route with `mode = preinstall`.
- WebUI service tarball: `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz`, size `2,845,730`, SHA256 `2C991A1F5D6EF885C98F25AD5C3502A79D260830A7C106C96E77B53633359828`; package scan found no `desktop/` entries and `/app/` uses the desktop renderer static shell.
- macOS unsigned DMGs: GitHub Actions run `27412042545`; arm64 size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`; x64 size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`.
- Public release zip: `release-artifacts/EcoreX_0.1.11-public-release.zip`, size `5,648,452`, SHA256 `5826F726869ABC9907CC243800E1F4A2372DE6AB77B5A948CD4CFBC9443B1256`; generated with the current ready Web artifact while Windows remains pending signature.

## macOS Boundary
- The repository contains a GitHub Actions macOS DMG workflow for arm64 and x64.
- DMG files must be produced on macOS or a macOS runner, then written into `deploy/ecorex-site/manifest.json` with real size/hash before the download page marks them ready.
- Signing, notarization, and Gatekeeper validation remain separate from the Windows workstation path unless Apple signing secrets are configured in GitHub.

## Remaining Release Steps
- Complete Authenticode signing for the current rebuilt Windows installer; update final signed size/hash in docs and `deploy/ecorex-site/manifest.json`.
- Repeat signed installed smoke after signing.
- Push v0.1.11 branch to GitHub.
- Upload macOS unsigned DMGs only if the download page clearly marks signing/notarization/Gatekeeper as deferred.
- Regenerate `release-artifacts/EcoreX_0.1.11-public-release.zip` after Windows signing and/or macOS artifacts are ready; the current zip is Web-ready only.
- Deploy the latest Admin/download package to `https://www.ecoreai.cn/ecorex-agent/`.
- Run Linux `install-ecorex-web.sh` and `check-ecorex-web-release.sh` against the production host after the Web tarball is uploaded.

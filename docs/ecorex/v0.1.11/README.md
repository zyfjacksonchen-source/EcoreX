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

## macOS Boundary
- The repository contains a GitHub Actions macOS DMG workflow for arm64 and x64.
- DMG files must be produced on macOS or a macOS runner, then written into `deploy/ecorex-site/manifest.json` with real size/hash before the download page marks them ready.
- Signing, notarization, and Gatekeeper validation remain separate from the Windows workstation path unless Apple signing secrets are configured in GitHub.

## Remaining Release Steps
- Complete Authenticode signing for the current rebuilt Windows installer; update final signed size/hash in docs and `deploy/ecorex-site/manifest.json`.
- Repeat signed installed smoke after signing.
- Push v0.1.11 branch to GitHub.
- Run the macOS DMG workflow and download both DMG artifacts.
- Update the public manifest with real DMG size/hash/status.
- Regenerate `release-artifacts/EcoreX_0.1.11-public-release.zip`.
- Deploy the latest Admin/download package to `https://www.ecoreai.cn/ecorex-agent/`.
- Run Linux `install-ecorex-web.sh` and `check-ecorex-web-release.sh` against the production host after the Web tarball is uploaded.

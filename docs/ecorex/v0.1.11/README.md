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
- Current rebuilt Windows installer size: `117,572,805` bytes
- Current rebuilt Windows installer SHA256: `CF0C5FFAFDF8A0C7FC0991BDFCBE5609917375D232AA419AAFCAE6329321CD18`
- Authenticode: `NotSigned` for the current rebuilt installer; previous signed installer was superseded by later fixes.
- Installed Windows smoke: passed unsigned; app started, sidecar ready, `/auth/check` returned success, and `/api/tools` included `bash`, `web_fetch`, and `browser`.
- Renderer visual smoke: passed for auth, main, settings, abilities, light and dark modes.
- Production Admin API hotfix: deployed v0.1.11 code and confirmed `ecorex-desktop-v0.1.11` reaches the public capability-policy route with `mode = preinstall`.
- WebUI service tarball: `release-artifacts/EcoreX_0.1.11-web-linux-service.tar.gz`, size `2,846,017`, SHA256 `7C08D86502943275E40E1924D6283D5419C2A2BF769078EB9AABC9B3E3AE9FC2`; package scan found no `desktop/` entries and `/app/` uses the desktop renderer static shell.
- macOS unsigned DMGs: GitHub Actions run `27412042545`; arm64 size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`; x64 size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`.
- Public release zip: `release-artifacts/EcoreX_0.1.11-public-release.zip`, size `311,225,938`, SHA256 `92A344CA721ABE73703FC75418A94BB18E033C813D2B9860390CB7DBDDD49A05`; generated with the ready Web artifact and both macOS unsigned DMGs while Windows remains pending signature.
- Production download page: `https://www.ecoreai.cn/ecorex-agent/manifest.json` is live as v0.1.11; Windows is `pending-signature`, macOS is `ready-unsigned`, WebUI is `ready`.
- Production WebUI: `ecorex-web.service` is active on the server, bound to Docker bridge `172.18.0.1:9909`, and public `https://www.ecoreai.cn/ecorex-agent/app/` passes login, app, auth-check, version, and SSE checks.
- GitHub source sync: SSH push is still blocked by local public-key access, but GitHub Git Data API synced the tool-permission source fix as snapshot `71c5578a889708aa8f652116084af055de61f2d9`; follow-up release notes are synced through the same API path.

## macOS Boundary
- The repository contains a GitHub Actions macOS DMG workflow for arm64 and x64.
- DMG files must be produced on macOS or a macOS runner, then written into `deploy/ecorex-site/manifest.json` with real size/hash before the download page marks them ready.
- Signing, notarization, and Gatekeeper validation remain separate from the Windows workstation path unless Apple signing secrets are configured in GitHub.

## Remaining Release Steps
- Complete Authenticode signing for the current rebuilt Windows installer; update final signed size/hash in docs and `deploy/ecorex-site/manifest.json`.
- Repeat signed installed smoke after signing.
- Keep GitHub refs current through GitHub API or a configured SSH key; direct SSH push is still not available on this Windows machine.
- Keep macOS unsigned DMGs clearly marked as signing/notarization/Gatekeeper deferred on the download page.
- Regenerate `release-artifacts/EcoreX_0.1.11-public-release.zip` after Windows signing is completed so the signed Windows installer can be included.
- Re-run public deployment only after Windows signing changes `windows-x64` from `pending-signature` to `ready`.

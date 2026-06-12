# EcoreX v0.1.11 Notes

## Scope
- Desktop UI/UX correction pass for the Codex-style two-column shell.
- WebUI-compatible SSE restoration for streaming reasoning, tool calls, media, cancellation, and completion events.
- Long assistant replies collapse by default and can be expanded inline.
- Factory persona defaults to professional, rigorous, restrained; address the user as "同学"; external identity is always EcoreX.
- Admin API and desktop enterprise policy move to the v0.1.11 client channel while retaining v0.1.10 compatibility.

## Current Evidence
- Windows installer: `desktop/release/EcoreX_0.1.11_x64-setup.exe`
- Windows installer size: `117,469,216` bytes
- Windows installer SHA256: `5ADF10F90DB64E46C6A92CB9FC0730F0A37D0C45B2F55B7EC566E25CF12E3685`
- Authenticode: `Valid`
- Installed Windows smoke: passed; app started, sidecar ready, `/auth/status` returned success.
- Renderer visual smoke: passed for auth, main, settings, abilities, light and dark modes.
- Production Admin API hotfix: deployed v0.1.11 code and confirmed both `ecorex-desktop-v0.1.10` and `ecorex-desktop-v0.1.11` client keys reach the public capability-policy route.

## macOS Boundary
- The repository contains a GitHub Actions macOS DMG workflow for arm64 and x64.
- DMG files must be produced on macOS or a macOS runner, then written into `deploy/ecorex-site/manifest.json` with real size/hash before the download page marks them ready.
- Signing, notarization, and Gatekeeper validation remain separate from the Windows workstation path unless Apple signing secrets are configured in GitHub.

## Remaining Release Steps
- Push v0.1.11 branch to GitHub.
- Run the macOS DMG workflow and download both DMG artifacts.
- Update the public manifest with real DMG size/hash/status.
- Regenerate `release-artifacts/EcoreX_0.1.11-public-release.zip`.
- Deploy the latest Admin/download package to `https://www.ecoreai.cn/ecorex-agent/`.

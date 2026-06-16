# EcoreX v0.1.13 Release Manifest

## Source And Build Basis

- Working branch: `codex/ecorex-v0.1.13`
- Local source commit for release infrastructure: `a9a97bc7a95265a63d69d5f93047683a52dd60a1`
- Remote branch commit used for the latest macOS DMG workflow: `a9a97bc7a95265a63d69d5f93047683a52dd60a1`
- macOS workflow: `.github/workflows/ecorex-desktop-release.yml`
- Build macOS Apps validation: GitHub Actions `macos-15` workflow_dispatch run `27611830980`
- macOS workflow inputs: `mac_arch=all`, `notarize=false`, `release_tag=v0.1.13`
- Result: `macOS DMG (arm64)` success and `macOS DMG (x64)` success; both DMGs were later materialized under the public download host after SHA256 verification.

> 2026-06-16 post-RC hotfix status: source commit `a9a97bc` contains the
> sidecar-readiness, hidden-context, and macOS-WebUI-ZIP fixes. The macOS DMGs
> and WebUI packages below have been rebuilt from that source. The Windows
> installer and public release zip must still be regenerated after a successful
> elevated SimplySign signing pass.

## Artifact Status

| Artifact | Status | Size | SHA256 |
| --- | --- | ---: | --- |
| `EcoreX_0.1.13_x64-setup.exe` | `pending-validation`, post-hotfix rebuild not signed yet | 149,193,112 | `D44E562E9874CAF7E9F2519FCDDE8A9EAC6A8E4D401956AB9672B4A051D4634B` |
| `EcoreX_0.1.13-webui-windows-x64.zip` | `ready` | 72,884,909 | `88F37BCD6C65C194398FF2248837F3BB0D3D6AE095859929558A813EFB40C61F` |
| `EcoreX_0.1.13-webui-macos-universal.zip` | `ready` | 165,308,769 | `3E4EA259222133E4AA7B08B99CFB4FC2E016B29612DC7C1488D0CC7E228DD3AE` |
| `EcoreX_0.1.13-webui-win-mac.zip` | `archived` | 238,356,152 | `7CFA6F123F96CCF94E2565E9CCA4F2CBE5871E1F054ECEB20265E4B9A3FD5AD4` |
| `EcoreX_0.1.13-web-linux-service.tar.gz` | `archived` | 3,129,472 | `84C24E9EC7AAD64313254610AABAAE336284CE28CD0FB4AFBACCE096AD55989E` |
| `EcoreX_0.1.13_arm64.dmg` | `ready-unsigned` | 192,665,255 | `CC3B7EE4FB48E8A4739E210BCC621F7F60D635D71183E78B1DEBB24F37AA0AFB` |
| `EcoreX_0.1.13_x64.dmg` | `ready-unsigned` | 200,039,732 | `B93957C3B0C662E63529B503BA29B9123C66584A2C87E2CAA68B7E29A1A7F8BC` |
| `EcoreX_0.1.13-public-release.zip` | pending regeneration | - | - |

## macOS DMG Notes

- The v0.1.13 DMGs are complete GitHub Actions build outputs, not locally renamed artifacts.
- They are intentionally unsigned/unnotarized for this release pass by user decision.
- The download page must show the Gatekeeper recovery hint: open System Settings, go to Privacy & Security, and click Still Open for EcoreX.
- The final public release zip embeds these DMGs under `site/downloads/`, so the download page does not depend on private GitHub Release asset URLs.

## Windows Signing Result

- The post-hotfix `release/win-unpacked/EcoreX.exe` has been rebuilt and currently requires an elevated SimplySign signing pass before NSIS packaging can be finalized.
- `Get-AuthenticodeSignature desktop/release/win-unpacked/EcoreX.exe` must report `Valid` before rebuilding `EcoreX_0.1.13_x64-setup.exe`.
- The final setup must be timestamped by DigiCert and verified with `Get-AuthenticodeSignature desktop/release/EcoreX_0.1.13_x64-setup.exe`.
- `certutil -user -key -csp "SimplySign CSP"` can still report no key containers even when elevated `signtool` works, so use direct elevated signtool signing as the release truth and keep the preflight as a diagnostic only.

## WebUI macOS Boundary

- The macOS WebUI package no longer ships `Install EcoreX WebUI.command`.
- It ships `Install EcoreX WebUI.app`, which starts the local runtime in the background and opens the browser after the service is ready.
- Logs go under `~/Library/Application Support/EcoreX WebUI/state/`.
- The package validator rejects terminal-opening `.command` entrypoints for the macOS WebUI package.
- The installer must start `app.py` with working directory set to the installed `runtime` directory. A macos-15 smoke run caught the previous bug where `config.json` was written under `runtime/` but the process launched from another cwd, so the WebUI ignored the selected port and fell back to `9899`.

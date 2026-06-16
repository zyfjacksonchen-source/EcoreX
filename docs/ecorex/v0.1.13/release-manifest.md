# EcoreX v0.1.13 Release Manifest

## Source And Build Basis

- Working branch: `codex/ecorex-v0.1.13`
- Local source commit for release infrastructure: `bdc867e400f2a2a80ca010537d4e71540e14eaf9`
- Remote branch commit used for the latest macOS DMG workflow: `92ce74cd828a95d5dace1a9c1bff1470092116cd`
- macOS workflow: `.github/workflows/ecorex-desktop-release.yml`
- Build macOS Apps validation: GitHub Actions `macos-15` workflow_dispatch run `27604509625`
- macOS workflow inputs: `mac_arch=all`, `notarize=false`, `release_tag=v0.1.13`
- Result: `macOS DMG (arm64)` success and `macOS DMG (x64)` success

## Artifact Status

| Artifact | Status | Size | SHA256 |
| --- | --- | ---: | --- |
| `EcoreX_0.1.13_x64-setup.exe` | `pending-signature` | 149,173,291 | `2C7140AD0E8A50663F7AE70607FF39F8B08E7D0D5F67C45991EF6B8288A854A9` |
| `EcoreX_0.1.13-webui-windows-x64.zip` | `ready` | 72,884,116 | `CEDD28AA0033031F7B70865D88CF7D5174F6B0D504D69AD27918854EFAF02675` |
| `EcoreX_0.1.13-webui-macos-universal.tar.gz` | `ready` | 79,808,141 | `21311D4BA1BC7F213D54BFBBF4431616B28EC19E228031CFD93CA3F14A9BAD67` |
| `EcoreX_0.1.13-webui-win-mac.zip` | `archived` | 153,045,004 | `8E91B8D3B667CD49E07F3C579FA630DEC4616E2D67A6766A5C7D1C016C1E59EC` |
| `EcoreX_0.1.13-web-linux-service.tar.gz` | `archived` | 3,129,472 | `84C24E9EC7AAD64313254610AABAAE336284CE28CD0FB4AFBACCE096AD55989E` |
| `EcoreX_0.1.13_arm64.dmg` | `ready-unsigned`, external | 192,665,001 | `EE1826474FBC99D0D54FF7FD09923BF82042C7816E9EE1864DD9532AFCD8549A` |
| `EcoreX_0.1.13_x64.dmg` | `ready-unsigned`, external | 200,043,508 | `517029EC4E716A92FF0F3BE98095AE2B892BF763070A4582520692551D114B86` |
| `EcoreX_0.1.13-public-release.zip` | deployment bundle | 154,788,854 | `4D58CBB0662A5D24908FB57315E349A0FBDC084F8919D5EE3E395D04BFA0504C` |

## macOS DMG Notes

- The v0.1.13 DMGs are complete GitHub Actions build outputs, not locally renamed artifacts.
- They are intentionally unsigned/unnotarized for this release pass by user decision.
- The download page must show the Gatekeeper recovery hint: open System Settings, go to Privacy & Security, and click Still Open for EcoreX.
- The public release zip references these DMGs through GitHub Release URLs instead of embedding 390+ MiB of DMG payload.
- The release validator and server checker must treat `external=true` HTTP(S) artifacts as ready metadata targets, not local `site/downloads` files.

## Windows Signing Boundary

- The current Windows NSIS setup is not public-ready because Authenticode is `NotSigned`.
- SimplySign/proCertum private key access must be checked from an elevated administrator process.
- A normal user shell may show `HasPrivateKey=True` while `signtool` still cannot see the CSP private-key container.
- Do not mark `windows-x64` as `ready` until the final v0.1.13 installer is signed and `Get-AuthenticodeSignature` reports `Valid`.

## WebUI macOS Boundary

- The macOS WebUI package no longer ships `Install EcoreX WebUI.command`.
- It ships `Install EcoreX WebUI.app`, which starts the local runtime in the background and opens the browser after the service is ready.
- Logs go under `~/Library/Application Support/EcoreX WebUI/state/`.
- The package validator rejects terminal-opening `.command` entrypoints for the macOS WebUI package.

# EcoreX v0.1.19 Installers

This repository contains public installer artifacts only. It must not contain source code, server deployment bundles, Admin API assets, internal scripts, or source archives.

## Artifact Policy

- Windows installers are publishable only when Authenticode verification is `Valid`.
- macOS DMGs may be published unsigned for v0.1.19 when marked `ready-unsigned` with `signature: unsigned`, SHA256, size, and install-smoke evidence.
- Unsigned Windows builds are internal hand-test candidates only and must not be marked ready.
- Hashes in `manifest.json`, `latest.yml`, `.blockmap`, and `SHA256SUMS.txt` must come from the final published files.

## Expected Files

- `EcoreX_0.1.19_x64-setup.exe`
- `latest.yml`
- `EcoreX_0.1.19_x64-setup.exe.blockmap`
- `EcoreX_0.1.19_arm64.dmg`
- `EcoreX_0.1.19_x64.dmg`
- `manifest.json`
- `SHA256SUMS.txt`
- `.gitattributes`
- `README.md`

## macOS Unsigned Install Note

The macOS DMG lane is intentionally unsigned for v0.1.19 if the manifest says `ready-unsigned`. Users may need to approve the app in macOS Privacy & Security or open it through Finder. This is not equivalent to notarized distribution and must be labeled as unsigned wherever it is shown.

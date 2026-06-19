# EcoreX Installers

This public repository is installer-only.

It must not contain EcoreX source code, scripts, server files, site/admin code, archives, or private deployment assets. Only binary installers, update metadata, checksums, manifest files, and this README are allowed.

## Downloads

- Windows x64: download `EcoreX_0.1.15_x64-setup.exe`
- macOS Apple Silicon: download the `arm64.dmg` asset when published
- macOS Intel: download the `x64.dmg` asset when published

Web deployment packages are not published in this installer-only repository. A WebUI artifact may be added later only if it is delivered as a separately verified no-source installer or image.

Always verify the SHA256 value in `SHA256SUMS.txt` or `manifest.json` before installing.

## Windows Install

1. Download the Windows setup executable.
2. Run the installer.
3. If Windows SmartScreen appears, confirm the publisher/signature and continue only if the SHA256 matches this repository.

## macOS Install

The current public macOS package may be unsigned/not notarized.

1. Download the `.dmg` for your CPU architecture.
2. Open the DMG and drag EcoreX to `/Applications`.
3. First launch may be blocked by Gatekeeper. Use one of these user-controlled options:
   - Finder: right-click `EcoreX.app`, choose `Open`, then confirm.
   - Terminal: run `xattr -dr com.apple.quarantine /Applications/EcoreX.app`, then open EcoreX normally.

Only do this for an installer whose SHA256 matches `SHA256SUMS.txt`.

## Agent Install Notes

Agents should download installers from this repository, never clone this repository expecting source code.

For macOS unsigned installs, agents should explain the Gatekeeper prompt and ask the user before running any quarantine-removal command.

## Release Files

- `manifest.json`: machine-readable installer list and SHA256 metadata.
- `SHA256SUMS.txt`: checksum list.
- `latest.yml` and `.blockmap`: Windows auto-update metadata when available.

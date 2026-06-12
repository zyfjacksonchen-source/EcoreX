# EcoreX v0.1.11 Release Manifest

## Local Artifacts
- Windows installer: `desktop/release/EcoreX_0.1.11_x64-setup.exe`
- Size: `117,469,216`
- SHA256: `5ADF10F90DB64E46C6A92CB9FC0730F0A37D0C45B2F55B7EC566E25CF12E3685`
- Signature: Authenticode `Valid`

## Public Manifest State
- Product: `EcoreX`
- Version: `0.1.11`
- Windows: ready locally and recorded in manifest.
- macOS arm64 DMG: pending real artifact.
- macOS x64 DMG: pending real artifact.

## Build Notes
- Standard `npm run package:win:signed` rebuilt runtime and renderer but hit an Electron Builder network timeout during full directory packaging.
- The release was safely recovered by updating the existing `win-unpacked` package with the latest `dist`, `dist-electron`, `package.json`, and staged runtime, repacking `app.asar`, signing core executables, generating NSIS from the prepackaged directory, and signing the installer.
- Installed-app smoke passed after that packaging path.

## Server Notes
- Production Admin API source and Docker build context were updated to v0.1.11.
- Docker Compose service `ecorex-admin-api` was rebuilt and is healthy.
- `ECOREX_CLIENT_EVENT_KEYS` now includes both `ecorex-desktop-v0.1.10` and `ecorex-desktop-v0.1.11`.

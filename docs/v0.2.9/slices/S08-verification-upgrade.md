# S09 Verification and Upgrade

## Status

Completed.

## Intent

Run focused checks and validate online upgrade from v0.2.8 to v0.2.9.

## Decisions

- Do not run `scripts/真实发布校验.py`.
- Validate v0.2.8 to v0.2.9 online upgrade through a focused smoke path.

## Implementation

- Built local v0.2.9 WebUI packages with `scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.2.9 -SkipCombinedPackage`.
- Produced and copied public WebUI artifacts:
  - `release-artifacts/EcoreX_0.2.9-webui-windows-x64.zip`
  - `release-artifacts/EcoreX_0.2.9-webui-macos-universal.zip`
  - `deploy/ecorex-site/downloads/EcoreX_0.2.9-webui-windows-x64.zip`
  - `deploy/ecorex-site/downloads/EcoreX_0.2.9-webui-macos-universal.zip`
- Promoted `deploy/ecorex-site/manifest.json` WebUI Windows/macOS artifacts from `pending-build` to `ready` using real size/SHA.
- Downloaded the v0.2.8 Windows WebUI package from the v0.2.8 release mirror and verified its documented size/SHA before using it as the legacy package.
- Ran local online upgrade smoke from 0.2.8 to 0.2.9 with a local `http://127.0.0.1:9808` static release site and `ECOREX_RELEASE_MANIFEST_URL` pointing at the local v0.2.9 manifest.
- Fixed `scripts/smoke-v028-legacy-webui-online-upgrade.ps1` so the downloaded online installer receives `-BaseUrl $BaseUrl`; without this, the installer fell back to the default production manifest.

## Evidence

- Windows WebUI artifact:
  - Size: `550795622`
  - SHA256: `3323BD22C920C7AA5CD42D4F42D2C1F8322CF76BCF08DD2F90CEDE5EC813FC73`
- macOS WebUI artifact:
  - Size: `652254333`
  - SHA256: `6EEC23D9FB9781F7699BE1B8D7FB5F1EEE5F502861B2DD5107D31F629A14F7E1`
- Upgrade smoke report:
  - `docs/v0.2.9/artifacts/legacy-webui-online-upgrade.json`
  - Status: `PASS`
  - Legacy version: `0.2.8`
  - Target version: `0.2.9`
  - Checks: `4/4` pass
  - Upgraded runtime: `runtime-0.2.9-b33f92af`

## Verification

- `npm run build:renderer` in `desktop/` passed before packaging.
- Local package structure check confirmed both v0.2.9 WebUI zip files include `release.json` and installer scripts.
- `docs/v0.2.9/artifacts/legacy-webui-online-upgrade.json` passed.
- `scripts/真实发布校验.py` was not run.

## Residual Notes

- The first smoke attempt failed because the online installer did not receive `-BaseUrl` and therefore read the production v0.2.8 manifest; this was fixed and rerun successfully.
- The upgrade smoke temp root `C:\ecx-upgrade-smoke-v029` could not be fully removed because Edge cache files under the isolated test profile remained locked by the OS/browser. This does not affect release evidence; the retained report is under `docs/v0.2.9/artifacts`.

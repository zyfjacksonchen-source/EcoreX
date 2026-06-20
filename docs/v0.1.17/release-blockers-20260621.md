# EcoreX v0.1.17 Release Blockers - 2026-06-21

## Current Gate

- `python scripts\check-ecorex-v0.1.17-promotion-gate.py --output docs\v0.1.17\promotion-gate.json --allow-no-go`
- Result: `NO-GO`, 24 checks, 5 blockers, 0 warnings.

## Blockers

| Blocker | Current Evidence | Recovery Command |
| --- | --- | --- |
| STAB-003 file symlink execution | `npm run smoke:local-path:symlink:win -- -OutputPath ..\docs\v0.1.17\local-path-safety-smoke.json` still fails because this Windows user cannot create file symlinks: `EPERM` / administrator privilege required. Non-symlink path checks pass. | Run from elevated PowerShell or enable Windows Developer Mode, then rerun `cd C:\CowAgent\desktop; npm run smoke:local-path:symlink:win -- -OutputPath ..\docs\v0.1.17\local-path-safety-smoke.json` |
| Windows signing provider | `scripts\dispatch-ecorex-windows-signed-release.ps1 -PreflightOnly` finds the certificate but fails because `SCardSvr` is stopped and SimplySign CSP exposes no key containers. `SimplySignDesktop` and `proCertumSmartSign` processes are running, but the provider is not ready. | Unlock Certum SimplySign/proCertum and start Smart Card services from elevated shell if needed, then rerun `powershell -ExecutionPolicy Bypass -File scripts\dispatch-ecorex-windows-signed-release.ps1 -PreflightOnly` |
| Windows signed installer/smoke | `docs\v0.1.17\win-installed-smoke.json` is missing and `deploy\ecorex-site\manifest.json` keeps `windows-x64` as `pending-signature`. | After preflight passes, run `powershell -ExecutionPolicy Bypass -File scripts\dispatch-ecorex-windows-signed-release.ps1 -ImportManifest`; the script signs/packages, runs installed smoke, and imports manifest evidence |
| macOS DMG artifacts | `macos-arm64-dmg` and `macos-x64-dmg` remain `pending` in the public manifest. Local workflow YAML parses and contains `workflow_dispatch`, arm64/x64 matrix, `smoke-macos-dmg.sh`, SHA256, and install-smoke upload paths. | Authenticate GitHub CLI, push/dispatch the workflow from the release branch, then run `powershell -ExecutionPolicy Bypass -File scripts\dispatch-ecorex-macos-dmg-workflow.ps1`; after it downloads artifacts, run the import command it prints |
| GitHub workflow dispatch | `gh auth status` reports no logged-in GitHub hosts; no `GH_TOKEN` environment was available in this shell. | Authenticate with `gh auth login` or provide a token through the shell environment. Use `powershell -ExecutionPolicy Bypass -File scripts\dispatch-ecorex-macos-dmg-workflow.ps1 -DryRun` to inspect the exact safe command without exposing tokens |

## Passed In This Slice

- `npm run smoke:local-path:symlink:win` refreshed STAB-003 evidence and correctly kept the gate blocked when symlink creation is unavailable.
- `npm run sign:win:preflight` was rerun after launching SimplySign/proCertum; failure remains external/provider readiness, not missing scripts.
- `.github/workflows/ecorex-desktop-release.yml` parses as YAML and exposes the `macos-dmg` job.
- `bash -n desktop/scripts/smoke-macos-dmg.sh desktop/scripts/validate-mac-artifacts.sh desktop/scripts/stage-runtime-mac.sh` passed.
- `scripts/dispatch-ecorex-macos-dmg-workflow.ps1` parses, dry-runs, refuses token arguments, dispatches only through authenticated `gh` or environment token, watches the run, downloads `ecorex-macos-arm64` / `ecorex-macos-x64`, and prints the manifest import command.
- `scripts/dispatch-ecorex-windows-signed-release.ps1` parses and dry-runs the full Windows chain: signing preflight, signed package, installed smoke, and manifest import. Real `-PreflightOnly` currently stops at the same Smart Card/SimplySign provider readiness blocker.

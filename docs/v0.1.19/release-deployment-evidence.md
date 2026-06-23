# EcoreX v0.1.19 Hotfix Release Deployment Evidence

Date: 2026-06-22

## Hotfix Trigger

- User-reported symptom: released Windows desktop app opened to a blank white renderer.
- Root cause: `ArtifactShelf` rendered a `useEffect` after an early `return null`; once v0.1.19 artifact verification hid all shelf items, React hit minified error #300 (`Rendered fewer hooks than expected`) and the renderer stayed blank.
- Code fix commit: `29025b972501ebd829599cec43ad3b87bb028919`.
- Regression guard: Windows installed smoke now starts Electron with a remote debugging port and requires a non-empty renderer DOM.

## Public Release Package

- Package: `release-artifacts/EcoreX_0.1.19-public-release.zip`
- Size: `985858391`
- SHA256: `FB586DBE155E2938860C2D551426974607BE66213D2968BFBF873E444C0E6754`
- Local validation: `scripts/prepare-ecorex-public-release.ps1 -Version 0.1.19` passed `scripts/validate-ecorex-release-artifacts.py`.

## Server Deployment

- Host: `ubuntu-140-143-183-53`
- Install command used expected SHA256: `FB586DBE155E2938860C2D551426974607BE66213D2968BFBF873E444C0E6754`
- Release directory: `/srv/ecorex-agent-download/releases/20260622153152-v0.1.19`
- Current symlink: `/srv/ecorex-agent-download/current`
- Server check: `scripts/check-ecorex-server-release.sh` passed.

## Public Verification

- Manifest: `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned `EcoreX 0.1.19` with hotfix notes.
- Public root: `https://www.ecoreai.cn/ecorex-agent/` returned HTTP 200.
- Admin gate: `https://www.ecoreai.cn/ecorex-agent/admin/` returned HTTP 401 unauthenticated.
- Client gates: `/client/model-config` and `/client/capability-policy` returned HTTP 403 unauthenticated.

## Windows Installed Smoke

- `EcoreX_0.1.19_x64-setup.exe`: Authenticode `Valid`, size `157452928`, SHA256 `E40DA22FBB6B29D7B8E78D08B5C884A65F8B339514CD20F3D3C325E814F6C3D6`.
- x64 installed smoke: `installed=true`, `rendererReady=true`, `sidecarReady=true`, `authReady=true`, `authNegativeReady=true`, `cleaned=true`, `rendererRootHtmlLength=57449`, `rendererBodyTextLength=375`.
- `EcoreX_0.1.19_ia32-setup.exe`: Authenticode `Valid`, size `103631384`, SHA256 `6BB2579510B1280EE957396C24D8380E94E390CB15F908210EA4FF5A1BC29239`.
- ia32 installed smoke: `installed=true`, `rendererReady=true`, `sidecarReady=true`, `authReady=true`, `authNegativeReady=true`, `cleaned=true`, `rendererRootHtmlLength=309`, `rendererBodyTextLength=16`.

## Windows Rebuild Status

- 2026-06-23 check for rebuilding signed Windows installers from the latest
  `codex/ecorex-v0.1.19` branch: `npm run sign:win:preflight` failed before
  packaging.
- Signing certificate is present in `Cert:\CurrentUser\My`, but the signing
  provider is not usable: Smart Card service `SCardSvr` and Certificate
  Propagation service `CertPropSvc` are stopped, and `certutil -user -key -csp
  "SimplySign CSP"` shows no visible key containers.
- Attempting to start `SCardSvr` and `CertPropSvc` from this non-elevated
  process failed with `Cannot open ... service on computer '.'`.
- `sign-win.ps1 -PreflightOnly -LaunchSimplySign` was also attempted; the
  provider remained unavailable. Rebuilding signed Windows installers requires
  unlocking Certum SimplySign/proCertum SmartSign and starting the Smart Card
  services in an elevated session.
- 2026-06-23 retry after the user opened GitHub billing/plan pages: `npm run
  sign:win:preflight` still failed. `SimplySignDesktop` was running, but
  `SCardSvr` and `CertPropSvc` remained stopped and no SimplySign CSP key
  containers were visible.

## Current Branch Web Rebuild

- Current branch head: `ee5b59449d270e2a9c64c0445eb4b739d7602fb0`.
- `npm run build` passed from `desktop/`, producing renderer chunk
  `assets/index-CcWYypJL.js` and compiling Electron TypeScript.
- `desktop/scripts/stage-runtime-win.ps1 -WinArch x64` passed and restaged
  `desktop/runtime/ecorex-runtime` from the repository root. This synchronized
  the network recovery changes into the runtime used by local WebUI and
  desktop packaging.
- Current-branch Web artifacts were generated under
  `release-artifacts/current-ee5b5944` for validation only. They were not
  uploaded to the public release because signed Windows installers and macOS
  DMGs have not yet been rebuilt from this same branch head.
- `EcoreX_0.1.19-web-linux-service.tar.gz`: size `3369259`, SHA256
  `3D83730384D769119E271031FE3FEA4CF761E9C375C69D6737B6D457EA79AEB5`.
- `EcoreX_0.1.19-webui-windows-x64.zip`: size `80952789`, SHA256
  `30721342262C9ED71A0A8FE501F4B171FB6A20AC85586C79FD05A060555C6938`.
- `EcoreX_0.1.19-webui-macos-universal.zip`: size `158064446`, SHA256
  `DF4F7614DD3CB099598DB0C1B40E9C3842F2552556843BCBB252DF0E56F5322D`.
- `EcoreX_0.1.19-webui-win-mac.zip`: size `239249838`, SHA256
  `71625F3554D0B2CA0B375867C8A7F4AD514723C7F1AF50BF49014564B7F4C4DD`.
- Package validation passed for the Linux service tarball with installed and
  HTTP checks disabled.
- Archive marker validation passed for all four current-branch Web artifacts:
  package runtimes contain `retry_mode`, `manual_retry_prepare`,
  `_last_model_retry_evidence`, and `retry_suppressed`, and all packaged
  renderer `index.html` files reference `assets/index-CcWYypJL.js`.

## Published Artifacts

- `EcoreX_0.1.19_x64-setup.exe`: size `157452928`, SHA256 `E40DA22FBB6B29D7B8E78D08B5C884A65F8B339514CD20F3D3C325E814F6C3D6`, GitHub asset digest `sha256:e40da22fbb6b29d7b8e78d08b5c884a65f8b339514cd20f3d3c325e814f6c3d6`.
- `EcoreX_0.1.19_ia32-setup.exe`: size `103631384`, SHA256 `6BB2579510B1280EE957396C24D8380E94E390CB15F908210EA4FF5A1BC29239`, GitHub asset digest `sha256:6bb2579510b1280ee957396c24d8380e94e390cb15f908210ea4ff5a1bc29239`.
- `EcoreX_0.1.19-webui-windows-x64.zip`: size `24288352`, SHA256 `F2E35868B5DCEF7D764CC5A0738C5AF38FB6C6EDE3A7040A9927D40639C79FDD`, GitHub asset digest `sha256:f2e35868b5dcef7d764cc5a0738c5af38fb6c6ede3a7040a9927d40639c79fdd`.
- `EcoreX_0.1.19-webui-macos-universal.zip`: size `158059391`, SHA256 `6A931438E7B1453FFAB65E709C5349D74492FBDE6F3531A024942A01A258D3E4`, GitHub asset digest `sha256:6a931438e7b1453ffab65e709c5349d74492fbde6f3531a024942a01a258d3e4`.
- `EcoreX_0.1.19-web-linux-service.tar.gz`: size `3366337`, SHA256 `7D022C03284BC984BADAB7C9AE205DF9903470E9062448E06297CE7955125477`, GitHub asset digest `sha256:7d022c03284bc984badab7c9ae205df9903470e9062448e06297ce7955125477`.
- `EcoreX_0.1.19_arm64.dmg`: size `213711048`, SHA256 `9F88702AD25B19EDD906DFA6EBEEB964D32F5D6F77898300124E15BA91DAD32C`, macOS install smoke `pass`, run `27950764344`.
- `EcoreX_0.1.19_x64.dmg`: size `221020283`, SHA256 `11E03C90B6822D83FF46BE9BE68DEF1026E8223D843E80A0523578F83474CC6A`, macOS install smoke `pass`, run `27951365387`.

## 2026-06-23 Windows/Web Refresh

- Scope: refreshed signed Windows desktop x64/ia32 installers and Web Win/Mac
  packages. macOS desktop DMGs were intentionally left as the existing
  v0.1.19 assets while macOS runner work remains paused.
- Signing tool: `C:\脚本签名工具\signtool.exe`, invoked through
  `desktop/scripts/sign-win.ps1` with provider preflight skipped for signing
  steps. A temporary signing probe succeeded before production packaging.
- Build command: `npm run package:win:all:signed` from `desktop/`.
- Windows x64 installed smoke passed: `installed=true`, `rendererReady=true`,
  `sidecarReady=true`, `authReady=true`, `authNegativeReady=true`,
  `cleaned=true`, `runtimeWinArch=x64`, `runtimePythonBits=64`,
  `rendererRootHtmlLength=104814`, `rendererBodyTextLength=1529`.
- Windows ia32 installed smoke passed: `installed=true`, `rendererReady=true`,
  `sidecarReady=true`, `authReady=true`, `authNegativeReady=true`,
  `cleaned=true`, `runtimeWinArch=ia32`, `runtimePythonBits=32`,
  `rendererRootHtmlLength=309`, `rendererBodyTextLength=16`.
- Refreshed publishable artifacts:
  - `EcoreX_0.1.19_x64-setup.exe`: size `157454656`, SHA256
    `6D74A30916318D86C2038727A6F60C82D07054E819DD051FED948CBFC8948D42`,
    Authenticode `Valid`.
  - `EcoreX_0.1.19_ia32-setup.exe`: size `103633640`, SHA256
    `6403F790408510B01B15EF502CBA7F7BFF0118148D6B48278B0AD1E130162B7C`,
    Authenticode `Valid`.
  - `EcoreX_0.1.19-webui-windows-x64.zip`: size `80952793`, SHA256
    `9C7D886FA4BB729C3470AEF68E449630B114E25D02ABED8E622A5E2E3BCA6568`.
  - `EcoreX_0.1.19-webui-macos-universal.zip`: size `158060910`, SHA256
    `9CDCB6B129D45403AECABC642C3E9433777EE47C32ECA754BBBE72C38C6EADC9`.
  - `EcoreX_0.1.19-web-linux-service.tar.gz`: size `3369246`, SHA256
    `A84F251D5BEA26DDAD2B695192180C59F365FB036ADD367656A5B9B923B612C1`.
- Public release package: `release-artifacts/EcoreX_0.1.19-public-release.zip`,
  size `1042061832`, SHA256
  `18D83C9491D5D62E02DB047D4A30A11A2E0D0D8BCE40C96541376E2F4AC2FE98`.
- Server deployment:
  - Install command used expected SHA256
    `18D83C9491D5D62E02DB047D4A30A11A2E0D0D8BCE40C96541376E2F4AC2FE98`.
  - Release directory:
    `/srv/ecorex-agent-download/releases/20260623002106-v0.1.19`.
  - `scripts/check-ecorex-server-release.sh` passed on the server.
  - `scripts/verify-ecorex-release.ps1 -SkipGitRemoteCheck` passed with zero
    blockers. Warnings were the intentional macOS desktop validation deferral
    and skipped authenticated enterprise policy checks.
- GitHub Release `v0.1.19` refreshed assets:
  - `EcoreX_0.1.19_x64-setup.exe`: digest
    `sha256:6d74a30916318d86c2038727a6f60c82d07054e819dd051fed948cbfc8948d42`.
  - `EcoreX_0.1.19_ia32-setup.exe`: digest
    `sha256:6403f790408510b01b15ef502cba7f7bff0118148d6b48278b0ad1e130162b7c`.
  - `EcoreX_0.1.19-webui-windows-x64.zip`: digest
    `sha256:9c7d886fa4bb729c3470aef68e449630b114e25d02abed8e622a5e2e3bca6568`.
  - `EcoreX_0.1.19-webui-macos-universal.zip`: digest
    `sha256:9cdcb6b129d45403aecabc642c3e9433777ee47c32eca754bbbe72c38c6eadc9`.
  - `EcoreX_0.1.19-web-linux-service.tar.gz`: digest
    `sha256:a84f251d5bea26ddad2b695192180c59f365fb036add367656a5b9b923b612c1`.

## macOS Hotfix Status

- Attempted hotfix DMG workflow: `27962802969` on commit `29025b972501ebd829599cec43ad3b87bb028919`.
- Result: failed before any job step started.
- GitHub annotation: the macOS jobs were not started because recent account payments failed or the spending limit needs to be increased.
- Follow-up attempt: `27965693068` on hotfix evidence commit `4cc799ae42797b0c7cf1b1af1d92d5fe1d0b561e`.
- Follow-up result: failed before any job step started with the same GitHub billing/spending-limit annotation.
- 2026-06-23 retry after the network-recovery follow-up commit: workflow run
  `27970105624` on branch `codex/ecorex-v0.1.19`, commit
  `e6d1f2f4366336a9e12e20f399fc93a8870f500a`.
- Retry result: failed before any job step started for both `macOS DMG (arm64)`
  and `macOS DMG (x64)`. GitHub run JSON shows empty `steps` arrays; failed
  log retrieval returned `log not found` because the jobs were rejected before
  runner execution.
- GitHub annotation remained: recent account payments failed or the spending
  limit needs to be increased in Billing & plans.
- 2026-06-23 second billing check after evidence commit: workflow run
  `27970406460` on branch `codex/ecorex-v0.1.19`, commit
  `82b25a3b385642a88cc0c4f49e9f979838c1a0ef`.
- Second check result: failed before any job step started for both `macOS DMG
  (arm64)` and `macOS DMG (x64)`. GitHub run JSON again shows empty `steps`
  arrays; failed log retrieval returned `log not found`.
- 2026-06-23 retry after the user opened GitHub billing/plan pages: workflow
  run `27971223382` on branch `codex/ecorex-v0.1.19`, commit
  `ee5b59449d270e2a9c64c0445eb4b739d7602fb0`.
- Retry result: failed before any job step started for both `macOS DMG
  (arm64)` and `macOS DMG (x64)`. GitHub run JSON again shows empty `steps`
  arrays; failed log retrieval returned `log not found`. The macOS runner
  billing/spending gate has therefore not recovered yet.
- Production note: Windows/Web hotfix assets were rebuilt and deployed. macOS desktop DMGs remain the existing v0.1.19 assets until GitHub macOS runner billing/spending is restored and the DMG workflow can be rerun.

## Notes

- Root Windows `latest.yml` remains canonical for x64; ia32 feed remains under `downloads/ia32/latest.yml`.
- The Windows release gate now rejects smoke evidence without `rendererReady=true`, positive `rendererRootHtmlLength`, and positive `rendererBodyTextLength`.

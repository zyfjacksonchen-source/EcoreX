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

## Published Artifacts

- `EcoreX_0.1.19_x64-setup.exe`: size `157452928`, SHA256 `E40DA22FBB6B29D7B8E78D08B5C884A65F8B339514CD20F3D3C325E814F6C3D6`, GitHub asset digest `sha256:e40da22fbb6b29d7b8e78d08b5c884a65f8b339514cd20f3d3c325e814f6c3d6`.
- `EcoreX_0.1.19_ia32-setup.exe`: size `103631384`, SHA256 `6BB2579510B1280EE957396C24D8380E94E390CB15F908210EA4FF5A1BC29239`, GitHub asset digest `sha256:6bb2579510b1280ee957396c24d8380e94e390cb15f908210ea4ff5a1bc29239`.
- `EcoreX_0.1.19-webui-windows-x64.zip`: size `24288352`, SHA256 `F2E35868B5DCEF7D764CC5A0738C5AF38FB6C6EDE3A7040A9927D40639C79FDD`, GitHub asset digest `sha256:f2e35868b5dcef7d764cc5a0738c5af38fb6c6ede3a7040a9927d40639c79fdd`.
- `EcoreX_0.1.19-webui-macos-universal.zip`: size `158059391`, SHA256 `6A931438E7B1453FFAB65E709C5349D74492FBDE6F3531A024942A01A258D3E4`, GitHub asset digest `sha256:6a931438e7b1453ffab65e709c5349d74492fbde6f3531a024942a01a258d3e4`.
- `EcoreX_0.1.19-web-linux-service.tar.gz`: size `3366337`, SHA256 `7D022C03284BC984BADAB7C9AE205DF9903470E9062448E06297CE7955125477`, GitHub asset digest `sha256:7d022c03284bc984badab7c9ae205df9903470e9062448e06297ce7955125477`.
- `EcoreX_0.1.19_arm64.dmg`: size `213711048`, SHA256 `9F88702AD25B19EDD906DFA6EBEEB964D32F5D6F77898300124E15BA91DAD32C`, macOS install smoke `pass`, run `27950764344`.
- `EcoreX_0.1.19_x64.dmg`: size `221020283`, SHA256 `11E03C90B6822D83FF46BE9BE68DEF1026E8223D843E80A0523578F83474CC6A`, macOS install smoke `pass`, run `27951365387`.

## macOS Hotfix Status

- Attempted hotfix DMG workflow: `27962802969` on commit `29025b972501ebd829599cec43ad3b87bb028919`.
- Result: failed before any job step started.
- GitHub annotation: the macOS jobs were not started because recent account payments failed or the spending limit needs to be increased.
- Follow-up attempt: `27965693068` on hotfix evidence commit `4cc799ae42797b0c7cf1b1af1d92d5fe1d0b561e`.
- Follow-up result: failed before any job step started with the same GitHub billing/spending-limit annotation.
- Production note: Windows/Web hotfix assets were rebuilt and deployed. macOS desktop DMGs remain the existing v0.1.19 assets until GitHub macOS runner billing/spending is restored and the DMG workflow can be rerun.

## Notes

- Root Windows `latest.yml` remains canonical for x64; ia32 feed remains under `downloads/ia32/latest.yml`.
- The Windows release gate now rejects smoke evidence without `rendererReady=true`, positive `rendererRootHtmlLength`, and positive `rendererBodyTextLength`.

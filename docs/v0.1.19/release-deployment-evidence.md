# EcoreX v0.1.19 Release Deployment Evidence

Date: 2026-06-22

## Public Release Package

- Package: `release-artifacts/EcoreX_0.1.19-public-release.zip`
- Size: `985857406`
- SHA256: `A7D7AFDF60F603B4367FB90C8B85F8190AD9F6128E27CF47986018C8E5D340C1`
- Local validation: `scripts/prepare-ecorex-public-release.ps1 -Version 0.1.19` passed `scripts/validate-ecorex-release-artifacts.py`.

## Server Deployment

- Host: `ubuntu-140-143-183-53`
- Install command used expected SHA256: `A7D7AFDF60F603B4367FB90C8B85F8190AD9F6128E27CF47986018C8E5D340C1`
- Release directory: `/srv/ecorex-agent-download/releases/20260622132924-v0.1.19`
- Current symlink: `/srv/ecorex-agent-download/current`
- Server check: `scripts/check-ecorex-server-release.sh` passed.

## Public Verification

- Manifest: `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned `EcoreX 0.1.19`.
- Public root: `https://www.ecoreai.cn/ecorex-agent/` returned HTTP 200.
- Admin gate: `https://www.ecoreai.cn/ecorex-agent/admin/` returned HTTP 401 unauthenticated.
- Client gates: `/client/model-config` and `/client/capability-policy` returned HTTP 403 unauthenticated.
- Release verifier: `scripts/verify-ecorex-release.ps1 -ExpectedVersion 0.1.19` reported `23` checks, `0` blockers.

## Published Artifacts

- `EcoreX_0.1.19_x64-setup.exe`: size `157451560`, SHA256 `E6BDF211B06DCF202C88ACEF6C4E0EC99F671819BA8F4B02A6ED1A70F9E5A32E`, Authenticode `Valid`.
- `EcoreX_0.1.19_ia32-setup.exe`: size `103631744`, SHA256 `E70C37EB873D8980DD87883731A5BE52A02E9DA779BE5610EF7938837F9D591A`, Authenticode `Valid`.
- `EcoreX_0.1.19_arm64.dmg`: size `213711048`, SHA256 `9F88702AD25B19EDD906DFA6EBEEB964D32F5D6F77898300124E15BA91DAD32C`, macOS install smoke `pass`, run `27950764344`.
- `EcoreX_0.1.19_x64.dmg`: size `221020283`, SHA256 `11E03C90B6822D83FF46BE9BE68DEF1026E8223D843E80A0523578F83474CC6A`, macOS install smoke `pass`, run `27951365387`.
- `EcoreX_0.1.19-webui-windows-x64.zip`: size `24288355`, SHA256 `51E5D7D7DAF738F92B7498582F762F79A8278559D0263A6524992E20C88E154F`.
- `EcoreX_0.1.19-webui-macos-universal.zip`: size `158059391`, SHA256 `0C2DF1709BC6B204E8E8FE1AAC22B3118367B10AB9BF8C7D7637CD5008AAD1D8`.
- `EcoreX_0.1.19-web-linux-service.tar.gz`: size `3366369`, SHA256 `1087915B2E4BFCE996D09D0B9D09E32B2587671731E2AB7A1A5A5CF27C50C723`.

## Notes

- Root Windows `latest.yml` was regenerated after ia32 packaging to keep the x64 update feed canonical.
- `desktop/package.json` now regenerates both x64 and ia32 update feeds after the full signed Windows build, preventing ia32 packaging from leaving the root feed pointed at the ia32 installer.

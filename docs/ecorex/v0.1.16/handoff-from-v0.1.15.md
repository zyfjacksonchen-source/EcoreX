# EcoreX v0.1.16 Handoff From v0.1.15

Date: 2026-06-19

## Current v0.1.15 Release State

- Do not replace the published v0.1.15 Windows installer unless the user explicitly asks again.
- The official local Windows release path was restored after the unsigned experiment:
  - `desktop/release/EcoreX_0.1.15_x64-setup.exe`
  - Authenticode status: `Valid`
  - SHA256: `87DE07DA7BCCB1193BDE8965398079B8F35B61E54E8880D24C8B41049F4056ED`
- A clean but unsigned Windows reference build was kept out of the release chain:
  - `release-artifacts/unsigned-clean-reference/EcoreX_0.1.15_x64-setup-unsigned-clean.exe`
  - `release-artifacts/unsigned-clean-reference/EcoreX_0.1.15_x64-setup-unsigned-clean.exe.blockmap`
  - `release-artifacts/unsigned-clean-reference/latest-unsigned-clean.yml`
- v0.1.15 WebUI Windows zip, WebUI macOS zip, Linux Web service tarball, and public download site were rebuilt/deployed with release scans passing for legacy product-name/CLI strings.
- macOS DMG download card was fixed on the download page; both arm64 and x64 DMG URLs returned HTTP 200 in server checks.
- The exact GitHub Release/tag `v0.1.15` remains deleted/absent. Do not recreate it.
- Baidu Netdisk upload was skipped by user decision.

## Signing Rule For v0.1.16

Use the user-specified signing script, not the desktop preflight signing flow:

```powershell
C:\脚本签名工具\signtool_sys双签.bat
```

The script expects files under:

```text
C:\脚本签名工具\all_files
```

Recommended signing sequence:

1. Build `desktop/release/win-unpacked` unsigned.
2. Copy core executable targets into `C:\脚本签名工具\all_files`, run the signing script, and copy the signed files back:
   - `EcoreX.exe`
   - `resources/elevate.exe`
   - `resources/ecorex-runtime/python/python.exe`
   - `resources/ecorex-runtime/python/pythonw.exe`
3. Build NSIS from the signed `win-unpacked`.
4. Copy the generated setup exe into `C:\脚本签名工具\all_files`, run the same signing script, and copy the signed setup back.
5. Verify with `Get-AuthenticodeSignature` and release scanners before updating manifests or public repos.

## Carryover For v0.1.16

- If v0.1.16 replaces the Windows installer, ensure the final signed setup has zero binary/text hits for:
  - `CowAgent`
  - `cow_cli`
  - `CowCli`
  - `COW_CLI`
  - `Cow CLI`
  - `C:\CowAgent`
  - `C:/CowAgent`
- Preserve the public installer-only repository rule: the public repo must contain only installers/packages, README, update metadata, manifest/checksums, and no source code.
- If using GitHub credentials again, do not print tokens and rotate any token pasted in chat.

# EcoreX Acceptance Audit - 2026-06-10

This document records the current acceptance state for the EcoreX desktop delivery goal.

It separates two gates:

- Release gate: signed Windows installer, macOS DMG artifacts, public download page, protected Admin page, GitHub source sync.
- Product gate: clean-machine first launch, real chat, file operations, Skill/MCP lifecycle, permissions, human-in-the-loop, admin policy enforcement, and multi-agent stability.

## Current Conclusion

Release gate: passed for the checks listed below.

Product gate: not fully passed yet. The development documents still require clean Windows/macOS machine validation, real Skill/MCP invocation, real permission and file-operation flows, and broader enterprise policy tests before the full product can be called final.

The active goal should stay open until the product gate is either verified or explicitly reduced by the product owner.

## Public Paths

- Download page: `https://www.ecoreai.cn/ecorex-agent/`
- Admin page: `https://www.ecoreai.cn/ecorex-agent/admin/`

No server IP, password, GitHub token, model API key, or client event key is stored in this audit document.

## Repeatable Release Verification

Script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-ecorex-release.ps1 `
  -LocalWindowsInstaller C:\CowAgent\desktop\release\EcoreX_0.1.4_x64-setup.exe `
  -LocalMacArm64Dmg C:\EcoreX\desktop\release\ecorex-macos-arm64\EcoreX_0.1.4_arm64.dmg `
  -LocalMacX64Dmg C:\EcoreX\desktop\release\ecorex-macos-x64\EcoreX_0.1.4_x64.dmg
```

Latest run:

- Time: 2026-06-10 18:33:05 +08:00
- Total checks: 14
- Blockers: 0
- Warnings: 0

Passed checks:

- Manifest returned product `EcoreX` and version `0.1.4`.
- Manifest contains Windows x64 installer, macOS Apple Silicon DMG, and macOS Intel DMG.
- Windows public download returned HTTP 200 and `Content-Length: 117524704`.
- macOS Apple Silicon public download returned HTTP 200 and `Content-Length: 152149407`.
- macOS Intel public download returned HTTP 200 and `Content-Length: 158353119`.
- Admin page without credentials returned HTTP 401.
- Client model-config without client key returned HTTP 403.
- Local Windows installer SHA256 matches manifest.
- Windows Authenticode signature is valid.
- Local macOS Apple Silicon DMG SHA256 matches manifest.
- Local macOS Intel DMG SHA256 matches manifest.
- Local Git HEAD matches GitHub `main`.

## Artifact Evidence

Windows:

- File: `EcoreX_0.1.4_x64-setup.exe`
- SHA256: `E2064B512B6038C06EB95AFD020BFF48F454221701D71247838834BF2DECC91F`
- Signature: valid
- Installed smoke test: passed

macOS Apple Silicon:

- File: `EcoreX_0.1.4_arm64.dmg`
- SHA256: `9F725653E78A7243675B36D46B62E123B8BF70CE34504919A33C56D1C6F8F992`
- Status: DMG generated and published

macOS Intel:

- File: `EcoreX_0.1.4_x64.dmg`
- SHA256: `BD25827B3982B4FF29208CDBD74222BFCAC7DCF6E181057D1EEC5E745DF00177`
- Status: DMG generated and published

## Objective Requirement Matrix

| Requirement | Evidence | Status |
| --- | --- | --- |
| Use the existing development docs and boundaries | `docs/ecorex-development-plan.md`, `docs/ecorex-dev-log.md`, `docs/ecorex-acceptance-checklist.md` reviewed during audit | Partially complete |
| Do not change agent core unnecessarily | Changes keep CowAgent-compatible internals and only adjust desktop mode behavior around WebChannel | Passed for current release scope |
| Windows installer signed with external signing flow | Authenticode valid; signing script does not import, delete, or manage client certificates | Passed |
| Do not delete client certificates | `desktop/scripts/sign-win.ps1` only invokes signing; no certificate delete/import cleanup logic | Passed |
| macOS directly outputs DMG | GitHub Actions produced arm64 and x64 DMG artifacts; both are published | Passed |
| Dual-platform download page deployed | Public manifest and all three download links return expected metadata | Passed |
| Admin page deployed with login | Admin route returns HTTP 401 without credentials; Admin API is protected behind the same route family | Passed |
| EcoreX path isolated from other projects | Caddy routes are scoped to `/ecorex-agent`; Admin API is its own compose service and database path | Passed for route scope |
| GitHub EcoreX repository replaced with latest source | Local HEAD equals GitHub `main` at `59f94b6c66c337892a8bf6725e482acad285b446` | Passed |
| Final answer must show domain, not IP | Public docs and final-facing paths use `https://www.ecoreai.cn/ecorex-agent/` | Passed |
| Clean Windows machine out-of-box use | Only local temporary install smoke has been verified; clean separate user machine not yet tested | Not verified |
| Clean macOS out-of-box use | DMGs exist, but clean macOS install, Gatekeeper, first launch, and real chat were not verified in this environment | Not verified |
| Skill installation and invocation | Capability-pack plumbing exists, but real Skill install/discover/invoke lifecycle is not yet end-to-end verified | Not verified |
| MCP setup and invocation | Existing MCP core is preserved, but desktop Settings to MCP connect/discover/invoke audit is not yet verified | Not verified |
| File read/write/edit/delete safety | Preview and core file support exist, but full permission-confirmation flows were not end-to-end verified | Not verified |
| Human-in-the-loop safety | UI and capability install cards exist, but all high-risk operation prompts were not fully exercised | Not verified |
| Admin model policy | Client model-config gate and active server-side model policy verified; key is not in public page or repository | Passed for global policy |
| Multi-agent performance and concurrency | No full stress test evidence yet | Not verified |

## Open Product-Gate Items

The following items are still required before marking the whole desktop product as finally accepted:

- Run Windows installer on a clean non-development Windows user machine.
- Run both macOS DMGs on clean Apple Silicon and Intel macOS machines.
- If macOS must be true enterprise out-of-box, add Apple Developer ID signing, notarization, staple, and Gatekeeper validation.
- Complete first-chat validation from a fresh desktop install using only Admin-provided model policy.
- Exercise Skill install, enable, discovery, invocation, failure recovery, and uninstall.
- Exercise MCP stdio and HTTP/SSE add, connect, discover, invoke, audit, disable, and failure flows.
- Exercise file preview plus read/write/edit/delete confirmation boundaries.
- Exercise web search and web fetch flows with source and failure handling.
- Exercise permission modes, revoke flow, and audit logging.
- Exercise human-in-the-loop for write/delete/send/external-action tasks.
- Exercise Admin user lifecycle, usage monitoring, error-log search, model policy update, release visibility, and capability policy.
- Exercise multi-agent concurrent runs for cancellation, UI responsiveness, sidecar isolation, and cost-loop protection.

## Maintenance Notes

- Use `scripts/verify-ecorex-release.ps1` before every public release update.
- Keep all secrets out of docs, source, manifest, and public HTML.
- If a token or model key is ever pasted into a thread, rotate it after deployment.
- Keep EcoreX public routes under `/ecorex-agent` to avoid impacting other hosted projects.
- Keep CowAgent compatibility keys for skills, MCP, and channel routing until alias migration is explicitly implemented and tested.


# EcoreX Release R8 Evidence - macOS DMG and Download Site

Date: 2026-06-10

## Scope

This record captures the current EcoreX desktop delivery state after adding macOS DMG artifacts to the public download page.

Product target:

- Windows and macOS desktop Agent delivered as EcoreX.
- Users should install and start chatting without local developer setup.
- Enterprise admins can configure model credentials and base URLs centrally.
- The agent core remains CowAgent-compatible internally where changing identifiers would break existing skills, MCP routing, or bot integrations.

## Public Entry Points

- User download page: `https://www.ecoreai.cn/ecorex-agent/`
- Admin page: `https://www.ecoreai.cn/ecorex-agent/admin/`

The Admin page and Admin API are protected by HTTP basic auth. The client model-config endpoint is protected by the EcoreX client event key and must not be exposed without that key.

## Desktop Artifacts

### Windows

- File: `EcoreX_0.1.4_x64-setup.exe`
- Public path: `/ecorex-agent/downloads/EcoreX_0.1.4_x64-setup.exe`
- Size: `117524704` bytes
- SHA256: `E2064B512B6038C06EB95AFD020BFF48F454221701D71247838834BF2DECC91F`
- Signing status: Authenticode valid in the local verification pass.
- Smoke status: Installed-app smoke test passed in the local verification pass.

### macOS arm64

- File: `EcoreX_0.1.4_arm64.dmg`
- Public path: `/ecorex-agent/downloads/EcoreX_0.1.4_arm64.dmg`
- Size: `152149407` bytes
- SHA256: `9F725653E78A7243675B36D46B62E123B8BF70CE34504919A33C56D1C6F8F992`
- Build source: GitHub Actions run `27267627049`
- Signing status: unsigned and not notarized.

### macOS x64

- File: `EcoreX_0.1.4_x64.dmg`
- Public path: `/ecorex-agent/downloads/EcoreX_0.1.4_x64.dmg`
- Size: `158353119` bytes
- SHA256: `BD25827B3982B4FF29208CDBD74222BFCAC7DCF6E181057D1EEC5E745DF00177`
- Build source: GitHub Actions run `27267627049`
- Signing status: unsigned and not notarized.

## Deployment Verification

Public manifest verification:

- `https://www.ecoreai.cn/ecorex-agent/manifest.json` returns Windows plus both macOS 0.1.4 entries.
- Manifest no longer points users to the older 0.1.1 macOS DMGs.

Public download headers:

- Windows installer returned HTTP 200 with `Content-Length: 117524704`.
- macOS arm64 DMG returned HTTP 200 with `Content-Length: 152149407`.
- macOS x64 DMG returned HTTP 200 with `Content-Length: 158353119`.

Admin/client route verification:

- Admin page without credentials returned HTTP 401.
- Admin API without credentials returned HTTP 401.
- Client model-config without the client event key returned HTTP 403.
- Client model-config with the server-side client event key returned configured model policy metadata without exposing the API key.

Remote file SHA256 verification:

- arm64 DMG: `9f725653e78a7243675b36d46b62e123b8bf70ce34504919a33c56d1c6f8f992`
- x64 DMG: `bd25827b3982b4ff29208cdbd74222bfcac7dcf6e181057d1eec5e745df00177`

## macOS Production Gate

The current macOS DMGs are useful for download-site plumbing and internal validation, but they do not yet satisfy the final macOS out-of-box standard.

Required before production:

- Add Apple Developer ID Application signing certificate to CI secrets.
- Add Apple notarization credentials to CI secrets.
- Enable hardened runtime-compatible signing for app and nested Electron helper code.
- Submit the packaged app or DMG for notarization.
- Staple the notarization ticket.
- Verify on a clean macOS machine that Gatekeeper allows first launch without manual override.
- Run real app startup, chat, model-policy pull, file preview, local file permission, and skill/MCP smoke tests on both Apple Silicon and Intel macOS.

Minimum validation commands for a notarized artifact:

```bash
codesign --verify --deep --strict --verbose=2 /Applications/EcoreX.app
spctl --assess --type execute --verbose /Applications/EcoreX.app
xcrun stapler validate /Applications/EcoreX.app
```

## Model Policy and Admin Notes

- Admins can create, update, delete, and activate model credentials centrally.
- Desktop clients poll the enterprise model policy and restart the sidecar when the effective policy changes.
- Admin list responses must keep API keys masked.
- The test model key and server-side client event key are operational secrets and must never be committed, printed in release notes, or shown in public pages.

## Remaining Acceptance Gaps

These items are still open before calling the whole desktop delivery production-ready:

- macOS signed and notarized DMG.
- Clean-machine macOS install and first-launch verification.
- Clean-machine Windows install and first-launch verification outside the development host.
- End-to-end chat using centrally managed model credentials from a fresh user account.
- First-use capability installation for Slack, Discord, Telegram, WeChat, DingTalk, voice, Playwright, Office/PDF heavy parsing, and related packs.
- Agent-driven capability recommendation and installation flow when the user asks for a task that needs a missing pack.
- Skill install, enable, discovery, invocation, failure recovery, and uninstall smoke tests.
- MCP connect, permission prompt, invocation, timeout, and error rollback smoke tests.
- Human-in-the-loop checkpoints for write/delete/send/external-action operations.
- Permission prompts that are clear, non-spammy, and still enforce file/network/admin safety boundaries.
- Admin usage monitoring, error-log search, user lifecycle, model policy update, and release visibility verification.
- Multi-agent concurrent task stress testing for UI responsiveness, sidecar process isolation, cancellation, and log correlation.

## Repository Notes

- Source repo target: `git@github.com:zhangyifanjackson-dotcom/EcoreX.git`
- The source tree was replaced with the current EcoreX/CowAgent-derived implementation while preserving the repository `.git` metadata.
- Local secret scans should be rerun before every push because the project interacts with model keys, GitHub credentials, and server deployment paths.


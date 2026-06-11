# EcoreX R9 - Permission Guard and Signing Reliability

Date: 2026-06-10

## Scope

This record covers the product-gate work after the release-gate audit.

Goal alignment:

- Move the desktop app closer to the real-user permission and human-in-the-loop requirements.
- Keep agent core behavior unchanged.
- Keep CowAgent-compatible internals intact.
- Improve Windows signing reliability so signing failures cannot be misreported as successful.

## Implemented

### Desktop Local Permission Guard

New file:

- `desktop/electron/permissions.ts`

Main process changes:

- `desktop/electron/main.ts`
- `desktop/electron/preload.ts`

Renderer changes:

- `desktop/src/App.tsx`
- `desktop/src/services/ecorexApi.ts`
- `desktop/src/vite-env.d.ts`
- `desktop/src/styles/app.css`

Behavior:

- Settings now exposes a Permissions panel.
- Supported modes:
  - `smart-ask`
  - `always-ask`
  - `read-only`
  - `custom`
- File picker selections are recorded as user-authorized local paths.
- Opening local paths now routes through the main-process permission guard.
- Smart Ask allows non-dangerous paths that were explicitly selected by the user.
- Executable or script-like files force a permission decision.
- Read Only mode blocks executable or script-like local opens.
- Users can revoke saved "always allow this kind" grants.
- Local permission decisions are written to an audit JSONL file under Electron `userData`.

This is a minimal product-gate step. It does not yet cover every file write/delete/send operation, but it gives the desktop shell a real permission state, persisted grants, and auditable local-open enforcement.

### Windows Signing Script Reliability

Updated:

- `desktop/scripts/sign-win.ps1`

Fix:

- After each `signtool sign` call, the script now checks `$LASTEXITCODE`.
- The script verifies the resulting Authenticode signature.
- If signing fails or verification is not valid, the script throws and exits non-zero.

Reason:

- During the 0.1.5 rebuild attempt, `signtool` printed an error but the previous script still returned success.
- That created a false-positive risk in the release pipeline.

## Verification

Passed:

- `npm run typecheck`
- `npm run build`
- `scripts/verify-ecorex-release.ps1` against the published 0.1.4 release:
  - total checks: 14
  - blockers: 0
  - warnings: 0
- Secret scan for GitHub tokens, model keys, server IPs, and server passwords in the source tree.

## 0.1.5 Packaging Attempt

The desktop package version was bumped to `0.1.5` for the permission-guard source work.

Windows installer generated locally:

- `desktop/release/EcoreX_0.1.5_x64-setup.exe`
- SHA256 before signing: `E192F07F8A8A93B00E8EB7F15D70168C47E0359DBA1229100B1C840D9DCB85D2`
- Signing status: not signed

Signing debug result:

- The certificate thumbprint matched.
- EKU filter passed.
- Expiry filter passed.
- Hash filter passed.
- Private Key filter left zero certificates.

Interpretation:

- The certificate exists in the user certificate store and has a private-key association from PowerShell's perspective.
- `signtool` cannot currently access the private key through SimplySign.
- This is most likely a SimplySign session/private-key unlock state issue, not source code or certificate deletion.

Release decision:

- Do not publish the 0.1.5 Windows installer until Authenticode is valid.
- Keep the public download page on the already verified signed 0.1.4 release.
- Keep the source changes because they improve product-gate behavior and signing reliability.

## Remaining Product-Gate Items

Still open:

- Clean Windows machine install with the new permission guard after a signed 0.1.5 package exists.
- macOS DMG build from the 0.1.5 source.
- macOS clean-machine first launch and Gatekeeper validation.
- Real file write/edit/delete confirmation flow.
- Real external-send confirmation flow.
- Skill install, enable, discovery, invocation, failure recovery, and uninstall.
- MCP add, connect, discover, invoke, audit, disable, and failure flow.
- Admin policy enforcement for permissions and capability preinstall on fresh desktop clients.
- Multi-agent concurrency and cancellation stress tests.

## Next Action

Before the next Windows package publish:

1. Unlock or re-authenticate SimplySign Desktop so `signtool` can access the private key.
2. Run `npm run sign:win:setup`.
3. Confirm `Get-AuthenticodeSignature` returns `Valid`.
4. Run `npm run smoke:win:installed`.
5. Update the public manifest only after the signed installer and smoke test pass.


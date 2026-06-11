# EcoreX v0.1.10 Runtime and Packaging Notes

## Windows Scope

- Windows packaging, signing, installation smoke test, and runtime startup are in scope for this round.
- Installed app must start the bundled runtime without Python, Node, Git, or manual environment variables on the user's machine.

## macOS Scope

- macOS code compatibility and packaging scripts must remain intact.
- macOS arm64/x64 signing, notarization, and Gatekeeper verification are skipped here and marked for later execution on a Mac.

## Startup Flow

1. Electron starts and loads persisted enterprise/admin policy.
2. User logs in or an existing user token is restored.
3. Electron refreshes model policy with user and device context.
4. A last-known-good model policy is used if the admin endpoint is temporarily unreachable.
5. The sidecar starts with the resolved runtime environment.
6. Renderer enters chat only after auth and runtime health are known.

## Enterprise Policy Packaging

- `stage-runtime-win.ps1` and `stage-runtime-mac.sh` stage `enterprise-policy.json` by default unless `ECOREX_DISABLE_ENTERPRISE_POLICY=1`.
- Default public policy:
  - `adminEventsUrl`: `https://www.ecoreai.cn/ecorex-agent/client/events`
  - `modelConfigUrl`: `https://www.ecoreai.cn/ecorex-agent/client/model-config`
  - `capabilityPolicyUrl`: `https://www.ecoreai.cn/ecorex-agent/client/capability-policy`
  - `clientEventKey`: `ecorex-desktop-v0.1.10`
- The default client event key is a public desktop channel marker, not a secret. It only opens login/policy transport routes; model credentials still require a valid enterprise user token.
- Environment variables can override the packaged policy at build time:
  - `ECOREX_ADMIN_BASE_URL`
  - `ECOREX_ADMIN_EVENTS_URL`
  - `ECOREX_MODEL_CONFIG_URL`
  - `ECOREX_CAPABILITY_POLICY_URL`
  - `ECOREX_CLIENT_EVENT_KEY`
  - `ECOREX_ORG_ID`

## Capability Install Flow

1. Renderer detects a requested capability from user intent or attachment type.
2. It asks Electron for pack state.
3. If missing and policy allows it, a fixed confirmation bar is shown above the composer.
4. Install progress and failures are displayed in the same area and logged for Admin error review.

## Release Metadata

- Version sources must be unified to `0.1.10`: desktop package files, manifest, release docs, and verification script defaults.
- Current Windows installer: `EcoreX_0.1.10_x64-setup.exe`, size `117,527,592` bytes, SHA256 `0AC3396261591F8433A36D13FF31FD47DFC4CB9E8119539AA2C188100661FD91`.
- Windows installed smoke passed after the enterprise policy packaging fix: install found, app started, sidecar ready, cleanup completed.

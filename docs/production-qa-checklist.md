# EcoreX Agent Production QA Checklist

This checklist covers the work that cannot be fully proven from a single development machine.

## Clean Machine Matrix

| Target | Required artifact | Required evidence |
| --- | --- | --- |
| Windows 10 x64 | `release/EcoreX Agent Setup <version>.exe` | Hash, SmartScreen/Defender result, first launch, login, model test, project create/switch, diagnostics export, uninstall, reinstall |
| Windows 11 x64 | `release/EcoreX Agent Setup <version>.exe` | Hash, SmartScreen/Defender result, first launch, permission UX, full access confirmation, concurrent cancel, uninstall, reinstall |
| macOS Apple Silicon | `EcoreX Agent-<version>-arm64.dmg` and `.zip` | Hash, Gatekeeper result, Keychain safeStorage, first launch, model test, project memory isolation, move to Applications, uninstall |
| macOS Intel | `EcoreX Agent-<version>-x64.dmg` and `.zip` | Hash, Gatekeeper result, Keychain safeStorage, first launch, model test, project memory isolation, move to Applications, uninstall |

Run:

```powershell
npm run verify:install-matrix
```

The command writes `release/install-matrix-report.json`. Attach screenshots and diagnostics exports to the row being certified.

## Real Model Long Task Stress

Use a disposable model key and a non-sensitive workspace.

```powershell
$env:ECOREX_REAL_MODEL_BASE_URL="https://model.example.com/"
$env:ECOREX_REAL_MODEL_API_KEY="<redacted>"
$env:ECOREX_REAL_MODEL_NAME="gpt-5.5"
$env:ECOREX_REAL_IMAGE_MODEL="image-2"
$env:ECOREX_REAL_AGENT_CONCURRENCY="2"
npm run test:real-agent
```

The script verifies:

- Model profile save, activate, and connection test.
- Concurrent Agent runs.
- Large prompt context.
- Cancellation.
- Diagnostics export and redaction.

Optional timeout probe:

```powershell
$env:ECOREX_REAL_AGENT_TIMEOUT_PROBE="1"
npm run test:real-agent
```

## macOS Packaging

On macOS with Xcode command line tools:

```bash
npm ci
npm run assets:mac-icon
npm run verify:mac
npm run dist:mac
```

Public distribution still requires:

- Developer ID Application certificate.
- Hardened runtime enabled.
- Notarization through Apple notary service.
- Gatekeeper validation with `spctl`.

## Staged Release And Rollback

Generate release policy metadata after packaging:

```powershell
$env:ECOREX_RELEASE_CHANNEL="stable"
$env:ECOREX_STAGED_ROLLOUT_PERCENT="10"
npm run release:policy
```

Emergency rollback metadata:

```powershell
$env:ECOREX_ROLLBACK_TO_VERSION="0.1.0"
$env:ECOREX_ROLLBACK_REASON="production incident"
npm run release:policy
```

The command writes `release/release-policy.json`. Publish this beside installer metadata when an update feed is introduced.

## Security Audit

```powershell
npm run audit:security
```

Policy:

- Production dependency vulnerabilities fail.
- Dev/runtime tooling high severity advisories are reported.
- Public release should upgrade Electron/electron-builder or document risk acceptance before signing.

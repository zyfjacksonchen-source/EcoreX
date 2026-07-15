# R24-02A Feishu/Lark External Connection Runtime Recovery

## Problem

User-visible symptom: Feishu app authorization is complete, Settings > External Connections has the correct App ID and Secret, but EcoreX reports `lark_oapi not installed`.

Initial code scan shows `lark-oapi>=1.5.5` is already declared in both root `requirements.txt` and `desktop/runtime-packs/core-requirements.txt`, so the slice should treat this as a runtime/environment/readiness mismatch until proven otherwise, not as a credential problem.

## Goal

Make Feishu/Lark external connection readiness EcoreX-native and production-clear:

- Correct credentials must not be blocked by a missing Python SDK in the active WebUI runtime.
- Missing SDK, wrong Python environment, packaging omission, or optional legacy registration mode must produce precise diagnostics and remediation.
- Manual App ID/Secret flow, one-click legacy SDK registration flow, and CLI/skill read-only flows must be clearly separated.
- No raw App ID, App Secret, tenant token, open IDs, chat IDs, or QR links may be written to logs/artifacts.

## Scope

In scope:

- WebUI dual-end runtime dependency detection and reporting.
- External connection test/status path for Feishu/Lark.
- Runtime pack and install/update contract for `lark-oapi`.
- A smoke that proves the same Python executable used by WebUI can import `lark_oapi`.
- A dry-run external connection smoke with redacted credentials.
- User-facing error copy that distinguishes SDK missing from bad credentials or missing app authorization.

Out of scope:

- Native desktop installer/signing/notarization work.
- Mutating Feishu data.
- Replacing `feishu_cli` / `@larksuite/cli` read-only tool flows.
- Using real secrets in committed artifacts.

## Initial Evidence

- Root `requirements.txt` declares `lark-oapi>=1.5.5`.
- `desktop/runtime-packs/core-requirements.txt` declares `lark-oapi>=1.5.5`.
- Local active WebUI runtime check on 2026-06-27:
  - `desktop/runtime/ecorex-runtime/python/python.exe` could not import `lark_oapi`.
  - `desktop/release/win-unpacked/resources/ecorex-runtime/python/python.exe` could not import `lark_oapi`.
- `scripts/prepare-ecorex-webui-local-release.ps1` currently filters `lark-oapi` out of macOS local core requirements and only writes an optional Windows notice via `Write-OptionalPythonDependencyNotice`.
- `scripts/smoke-v023-install-packaging-contracts.py` now encodes the safer expectation that Windows package build preinstalls `lark_oapi`, while first-run install does not synchronously run pip.
- `channel/feishu/feishu_channel.py` defaults Feishu event mode to `websocket`; that mode raises `lark_oapi not installed` before credential validity can matter.

Working diagnosis: the user likely has valid credentials, but the active WebUI runtime lacks the SDK required by websocket event mode or one-click legacy registration. This is a runtime dependency delivery/readiness issue, not a credential issue.

## Planned Implementation Slices

1. Diagnose the current runtime:
   - Identify which Python environment serves WebUI.
   - Check whether that environment can import `lark_oapi`.
   - Compare root requirements, runtime pack requirements, packaged runtime, and installed runtime.
   - Record the finding without exposing secrets.

2. Normalize readiness:
   - Add or repair a single EcoreX-native Feishu dependency/readiness probe.
   - The probe reports `sdkPresent`, `sdkVersion`, `pythonExecutableKind`, `credentialPresent`, and `mode`.
   - External Connections should consume this probe instead of surfacing raw `ImportError`.

3. Repair dependency delivery:
   - Replace the old “optional notice only” behavior with a production-safe install/verify path for flows that require `lark_oapi`.
   - Ensure WebUI runtime install/update paths install or explicitly block with precise remediation for `lark-oapi>=1.5.5` in the active runtime environment.
   - Ensure packaged/runtime manifests cannot claim Feishu ready if `lark_oapi` is missing.
   - Avoid duplicating dependency lists; reuse runtime-pack source of truth where possible.

4. Clarify flows:
   - Manual App ID/Secret connection should not require one-click registration unless the user explicitly starts that legacy path.
   - One-click registration may require `lark_oapi.register_app`; if absent, show a focused install/runtime message.
   - Read-only CLI/document access remains `feishu_cli` / `@larksuite/cli` and is separate from app event channel readiness.

5. Verify:
   - Unit/source contract for `lark-oapi` declared in runtime requirements.
   - Runtime import smoke using the WebUI Python executable.
   - External connection dry-run smoke: correct redacted credential presence plus SDK present/missing classification.
   - Browser/UI smoke: missing SDK copy is actionable and does not look like credential failure.
   - Privacy scan over all new artifacts.

## Multi-Agent Review Gate

The slice cannot PASS until independent reviewers agree on:

- Runtime/dependency root cause.
- External connection flow correctness.
- Packaging/update coverage for WebUI dual-end.
- Security/privacy handling of credentials and Feishu identifiers.
- User-facing diagnosis and recovery copy.

## Acceptance Criteria

- With valid App ID/Secret and `lark_oapi` installed in the active runtime, External Connections no longer reports `lark_oapi not installed`.
- If `lark_oapi` is missing, EcoreX reports the active runtime and exact remediation without implying the credentials are wrong.
- `lark-oapi>=1.5.5` is installed/validated through WebUI runtime pack/update paths.
- Manual credentials, one-click app registration, and read-only CLI access have separate readiness states.
- Artifacts prove import/readiness/dry-run behavior and pass privacy scanning.

## Implementation Evidence

Status: PASS. Local implementation, active-runtime smoke, browser smoke, targeted tests, package/release validation, privacy scans, ZIP-inspection contract, and five-agent consensus review all passed.

- Added `common/feishu_runtime_readiness.py` as a local-only readiness probe. It avoids network calls, does not expose raw executable paths, and uses metadata checks for ordinary UI refreshes so `lark_oapi` is not imported on every status request.
- Updated Feishu channel startup to check SDK availability dynamically instead of relying on stale import-time state.
- Updated External Connections backend and frontend to separate:
  - credentials configured
  - SDK/runtime dependency missing
  - credential validity unknown unless remotely tested
  - CLI/agent callable readiness
- Changed missing SDK UI state to `运行依赖缺失` with copy explaining that saved App ID/Secret are not a credential validation failure.
- Masked Feishu App ID/Secret values are not written back when the UI sends placeholder values.
- Feishu home channel IDs are projected as configured state plus HMAC digest and optional name, never as raw `oc_...` IDs.
- Legacy Feishu channel/message logs and host diagnostics log tails now redact Feishu credentials, IDs, QR URLs, data-image payloads, session references, local paths, response bodies, and full event/message contents; raw QR, webhook request, and message key/path/content log templates are absent from packaged WebUI artifacts.
- Packaging/update contracts now require active WebUI runtime installation/verification of `lark_oapi`; macOS local requirements no longer prune `lark-oapi`.
- Rebuilt the ready WebUI Windows/macOS ZIPs and public-release ZIP so `deploy/ecorex-site/manifest.json` no longer points at stale packages that omit `lark-oapi`.

Verification artifacts:

- `docs/v0.2.4/artifacts/feishu-lark-oapi-active-runtime.json`
- `docs/v0.2.4/artifacts/feishu-lark-oapi-external-connections-browser.json`
- `docs/v0.2.4/artifacts/feishu-lark-oapi-release-artifact-contract.json`
- `docs/v0.2.4/artifacts/feishu-lark-oapi-privacy.json`

Passing checks:

- `tests/test_v024_feishu_lark_oapi_recovery.py` (`8 passed` after adding log-redaction regression coverage)
- Targeted external-connection homeChannel and browser-smoke harness regression tests.
- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build:renderer`
- `scripts/smoke-v023-install-packaging-contracts.py`
- `scripts/smoke-v024-feishu-lark-oapi-runtime.py`
- `scripts/smoke-web-external-connections-browser.py`
- `scripts/validate-ecorex-release-artifacts.py --version 0.2.3`
- `scripts/scan-session-artifacts-privacy.py`

Known unrelated check:

- `scripts/smoke-web-hotfix-contracts.py` still fails legacy HFX-04 v0.2.2 version-surface assertions in this v0.2.3/v0.2.4 tree. The HFX-05 Feishu writeback and `lark_oapi` install checks pass.

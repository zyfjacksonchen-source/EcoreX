# EcoreX v0.2.2 Release Manifest

## Status

Status: `RELEASE-PASS`.

This manifest records the promoted Web-focused release evidence for v0.2.2. The
public Web manifest and local release defaults are promoted to v0.2.2, and the
target-environment Web Linux service deploy/rollback smoke has passed with
redacted evidence. The latest automated production online browser smoke remains
failed evidence, but the operator explicitly skipped that smoke for this goal;
the skip is recorded in a hash-only waiver artifact and is machine-validated by
the release gate.

## Artifact Inventory

| Artifact ID | Version | Path | Size | SHA256 | Status |
| --- | --- | --- | ---: | --- | --- |
| `web-linux-service` | `0.2.2` | `release-artifacts/EcoreX_0.2.2-web-linux-service.tar.gz` | 3679009 | `3BEA1EF91C61E9E42235AE7695DDAEBEF25B4A6C5B13B6726240539CC937CCF7` | `VALIDATED` |
| `webui-windows-x64` | `0.2.2` | `release-artifacts/EcoreX_0.2.2-webui-windows-x64.zip` | 83385608 | `BE25FAE0B33DAFF66EA7C0749B21A6F3198C021C43B87DA3123F3666E41A96F1` | `VALIDATED` |
| `webui-macos-universal` | `0.2.2` | `release-artifacts/EcoreX_0.2.2-webui-macos-universal.zip` | 175711756 | `2FD49E130040CAF5E98F2038465441A21F3A995B41316F9493C68299A8FDE261` | `VALIDATED` |
| `public-release` | `0.2.2` | `release-artifacts/EcoreX_0.2.2-public-release.zip` | 264864808 | `BFA0DD949907ECE14787FB5C1D32F3163C42E72ABFB9A83EF9A7BE8FE6DD5F7C` | `VALIDATED` |

SHA256 sidecar:

- `release-artifacts/EcoreX_0.2.2-web-linux-service.tar.gz.sha256`

## Build Inputs

Build command:

```powershell
pwsh -NoLogo -NoProfile -Command '& ./scripts/prepare-ecorex-web-release.ps1 -Version 0.2.2 -RuntimeRoot . -SiteRoot deploy/ecorex-site -OutputDir release-artifacts'
pwsh -NoLogo -NoProfile -Command '& ./scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.2.2 -RuntimeRoot desktop/runtime/ecorex-runtime -OutputDir release-artifacts'
pwsh -NoLogo -NoProfile -Command '& ./scripts/prepare-ecorex-public-release.ps1 -Version 0.2.2 -SiteRoot deploy/ecorex-site -OutputDir release-artifacts'
```

Build result:

- `version`: `0.2.2`
- `artifactId`: `web-linux-service`
- `webBuild`: `desktop-renderer-build`
- `includesDesktopArtifacts`: `false`
- Release runtime sanitizer: `PASS`
- Public release zip sanitizer/validator: `PASS`
- Hotfix rebuild includes session isolation, no-sweep status motion, Feishu write-back without raw-secret response, auth-check identity recovery, Codex-like new-session entry, artifact full-source dedupe, streaming Markdown smoothness, Codex-like font stacks, and the CowAgent-referenced `markdown-it` parity hotfix for long-answer/history/streaming Markdown.
- Public manifest promoted: `deploy/ecorex-site/manifest.json` version `0.2.2`, WebUI Windows/macOS and Web Linux service artifacts all point to v0.2.2 files and hashes.
- Release defaults promoted: Web install/check/public package scripts default to `0.2.2`.

## Promoted Release Evidence

Public manifest promoted and release defaults promoted are validated by
`scripts/check-v022-release-gate.py`. The same gate now directly validates the
production deploy and online browser smoke artifacts before returning
`--require-releasable` PASS. The gate also checks that a future successful
online browser smoke is newer than or equal to the production deploy evidence.
For this release goal, the failed online browser smoke is superseded by
`docs/v0.2.2/artifacts/online-web-browser-smoke-waiver.json`, which records the
operator-requested skip without raw target or secret data.

Local deploy/rollback smoke:

```powershell
pwsh -NoLogo -NoProfile -Command 'python scripts/smoke-v022-release-deploy-rollback.py --package release-artifacts/EcoreX_0.2.2-web-linux-service.tar.gz --artifact docs/v0.2.2/artifacts/release-deploy-rollback-smoke.json'
```

Smoke artifacts:

- `docs/v0.2.2/artifacts/release-deploy-rollback-smoke.json`
- `docs/v0.2.2/artifacts/release-target-deploy-rollback-smoke.json`
- `docs/v0.2.2/artifacts/release-target-command-template.json`
- `docs/v0.2.2/artifacts/production-deploy-online.json`
- `docs/v0.2.2/artifacts/online-web-browser-smoke.json`
- `docs/v0.2.2/artifacts/online-web-browser-smoke-waiver.json`
- `docs/v0.2.2/artifacts/goal-completion-audit.json`
- `docs/v0.2.2/artifacts/feishu-im-real-credential-smoke.json`

Validated local scope:

- Local filesystem extraction.
- Package structure, package sidecar SHA256, bundled `checksums.json`, bundled `SHA256SUMS.txt`, deploy pointer switch, rollback pointer switch, and candidate retention are verified.

Local smoke result summary:

- `status`: `PASS`
- `scope`: `local-filesystem-web-linux-service`
- `productionEnvironment`: `false`
- `requiresRoot`: `false`
- `requiresSystemd`: `false`
- `requiresNetwork`: `false`
- `pointerMethod`: `manifest-pointer-fallback`
- `checksumsJsonFilesVerified`: `489`
- `sha256SumsFilesVerified`: `489`
- `rollback.verified`: `true`

Target-environment deploy/rollback smoke passed:

- `status`: `PASS`
- `scope`: `target-environment-web-linux-service`
- `productionEnvironment`: `true`
- `requiresRoot`: `true`
- `requiresSystemd`: `true`
- `requiresNetwork`: `true`
- `pointerMethod`: `target-current-symlink`
- `preState.currentVersion`: `0.2.1`
- `deployState.currentVersion`: `0.2.2`
- `rollback.currentVersionAfterRollback`: `0.2.1`
- `rollback.candidateRetainedForAudit`: `true`
- `commands`: 11 redacted command rows in the exact ordered target smoke command chain: `prepare_remote_dir`, `upload_package`, `upload_installer`, `upload_checker`, `chmod_release_scripts`, `capture_pre_state`, `install_v022`, `check_deploy`, `capture_deploy_state`, `rollback_to_previous`, `capture_rollback_state`.
- Hash boundary: every persisted command row carries uppercase hex hash-shaped `argvHash`, `stdoutHash`, and `stderrHash` evidence; target identity fields are stored as `target.*Hash` evidence only.
- Redaction boundary: raw target values, raw commands, raw stdout/stderr, and raw secrets are not persisted.

Real Feishu/IM read-only credential smoke now passes through
`docs/v0.2.2/artifacts/feishu-im-real-credential-smoke.json`. It proves real
read-only authorization and chat-list reachability while intentionally omitting
message send/write coverage.

Production deploy passed through
`docs/v0.2.2/artifacts/production-deploy-online.json`:

- Web service final state: `0.2.2`, systemd active/enabled, local HTTP status `200`.
- Public manifest final state: `0.2.2`.
- Admin health final state: `0.2.2`.

Local React browser hotfix smoke passed through
`docs/v0.2.2/artifacts/r22-13-react-browser-smoke.json`,
`docs/v0.2.2/artifacts/r22-13-react-browser-smoke.png`, and
`docs/v0.2.2/artifacts/r22-13-react-browser-smoke-narrow.png` from
`scripts/smoke-web-hotfix-react-browser.py`:

- Markdown raw heading marker visible: `false`.
- `# 标签` renders to compact h1 at `19.6px`; `#世界杯 #看球 #宅家看球` remains body text at `14px`.
- Code block font: `12.25px`; code-block local/remote URL text remains inert and is not linkified or media-converted.
- Long-answer preview uses rendered DOM clipping: `overflow=hidden`, `maxHeight=544px`.
- Run timing visible in active and refreshed history views: `true`.
- Desktop and 390px narrow viewport horizontal overflow: `0`.

Production online browser smoke is currently waived through
`docs/v0.2.2/artifacts/online-web-browser-smoke.json`:

- Browser UI: explicit login identity visible, no local fallback identity, v0.2.2 visible, Codex-like new-session entry visible, project/general entries visible, Run Center hidden, system UI font stack applied.
- Real model-message completion/final timing did not pass because the automated account lacked a usable model path during the last online smoke.
- The operator explicitly requested skipping online smoke for this goal; `online-web-browser-smoke-waiver.json` binds that waiver to the current Web tarball, public release zip, and production deploy artifact by hash.

## Scope Notes

- Desktop package metadata is not promoted as part of this Web-focused v0.2.2 slice.
- The target release checker validated the Web service from the target environment; a separate public external URL probe was not configured in the persisted target artifact.
- The Feishu/IM smoke is read-only; message send/write behavior is outside this release evidence.
- `release-target-command-template.json` is a gate-validated handoff aid only. It records local package/script hashes and placeholders, not target execution.

## Release Gate Commands

Required preflight:

```powershell
pwsh -NoLogo -NoProfile -Command 'python scripts/check-v022-harness-matrix.py --json'
pwsh -NoLogo -NoProfile -Command '$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest tests/test_v022_harness_matrix.py -q'
pwsh -NoLogo -NoProfile -Command 'python scripts/check-v022-release-gate.py --json --artifact docs/v0.2.2/artifacts/release-gate-preflight.json'
pwsh -NoLogo -NoProfile -Command '$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest tests/test_v022_release_gate.py -q'
pwsh -NoLogo -NoProfile -Command 'python scripts/audit-v022-goal-completion.py --json --artifact docs/v0.2.2/artifacts/goal-completion-audit.json'
pwsh -NoLogo -NoProfile -Command '$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest tests/test_v022_goal_completion_audit.py -q'
```

Release command:

```powershell
pwsh -NoLogo -NoProfile -Command 'python scripts/check-v022-release-gate.py --json --require-releasable'
```

Expected current result:

- `PASS`, because public Web artifacts/defaults are promoted, target-environment deploy/rollback evidence is present, Feishu/IM read-only credential evidence is present, production Web/Admin deployment evidence is present, the online browser smoke skip is explicitly waived by operator instruction, the harness matrix is reviewed, and R22-12 is recorded as reviewed PASS.

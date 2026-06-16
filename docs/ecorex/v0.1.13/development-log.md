# EcoreX v0.1.13 Development Log

## Baseline

- Source branch: `codex/ecorex-v0.1.13`
- Required base: `ecorex/main` at v0.1.12 final commit `b76f5a0d8495c9d447078e1db56f06b56cc962d2`
- Public v0.1.12 download manifest remains the production source of truth until v0.1.13 artifacts are rebuilt, signed, hashed, and deployed.
- Do not commit `.tmp-*`, `desktop/release-local-*`, caches, secrets, or token files.

## Implemented Source Changes

### Composer Input Reliability

- Fixed session-switch races by tracking a monotonic switch sequence and ignoring stale history loads from a previous session.
- Updated new-session activation to refresh the active session ref before history/UI writes, preventing old async state from overriding the fresh composer.
- Hardened composer focus restore with immediate focus, animation-frame focus, and a short delayed retry.
- Kept composer height in sync after focus and after programmatic newline insertion.

### Multiline Shortcut

- `Ctrl + Enter` now inserts a newline instead of sending.
- macOS-compatible `Command + Enter` also inserts a newline.
- Plain `Enter` still sends the message, and `Shift + Enter` keeps the browser's native textarea newline behavior.

### Built-In Skills And Tools

- `skill-creator` already existed as a packaged built-in skill and remains enabled as an out-of-box ability.
- Added a real first-party `find` tool under `agent/tools/find/`.
- Added built-in `skills/find/SKILL.md` so agents know to use the structured finder before reading or shelling around.
- Added `find` to the desktop ability surface and default skill allowlist.
- Added release-validator sentinels so future packages must include both the `find` tool and the `find` skill.

### Non-Feishu CLI Preset

- The source already includes the project CLI under `cli/` and plugin entry points under `plugins/cow_cli/`.
- Added first-party `ecorex_cli` as a structured wrapper for selected safe project CLI commands:
  - `version`
  - `status`
  - `skill_list`
  - `skill_info`
  - `skill_search`
  - `skill_list_remote`
  - `skill_install`
  - `skill_enable`
  - `skill_disable`
  - `knowledge_status`
  - `knowledge_list`
  - `install_browser`
- Safe read-only actions run directly. Network and mutating actions go through the permission broker.
- Added `ecorex_cli` to tool configuration allowlists and the desktop ability surface.

### Update Notes

- Added `common/ecorex_release_notes.py` as the single runtime source for user-facing v0.1.13 update notes.
- `/api/version` now returns both `version` and `releaseNotes`.
- Desktop/WebUI renderer loads release notes through `loadRuntimeSnapshot()`.
- On first open after a new version, WebUI/Desktop shows a one-time update note modal with user-readable changes, fixes, how-to text, and update policy.
- The modal remembers the seen version in local storage and has an in-memory fallback to avoid repeated popups when storage is unavailable.

### Local Hand-Test Login Boundary

- A temporary local hand-test login bypass was used only for `release-local-0025` while the Admin API was still on v0.1.12.
- The bypass was removed before the release-candidate rebuild so the production desktop package cannot skip enterprise login through an environment variable.
- The production Admin API is v0.1.13 and keeps v0.1.10-v0.1.12 client keys accepted during rollout, so older clients continue to work after deploy.

### Download Page

- Added a macOS DMG install hint to the public download card: if macOS blocks the app, users should open "系统设置 → 隐私与安全性" and click "仍要打开" for EcoreX.
- Updated the download page `site.js` cache buster so browsers pick up the new prompt.
- Rebuilt and synced WebUI static assets for the v0.1.13 source pass: `index-CcCofcc7.js` and `index-D7oCsug3.css`.

### macOS WebUI Local Package

- Replaced the macOS WebUI `.command` entrypoint with `Install EcoreX WebUI.app` so normal WebUI use does not expose the Agent CLI/runtime terminal.
- The app launcher runs the install/start script in the background, writes stdout/stderr to `~/Library/Application Support/EcoreX WebUI/state/`, and opens the browser after the local service is ready.
- The macOS install script clears quarantine attributes from the extracted package and installed Python/runtime before dependency setup to reduce `Killed: 9` failures on downloaded packages.
- Dependency installation uses offline wheels with pip cache and bytecode compilation disabled to reduce first-run resource spikes.
- The release validator now rejects WebUI macOS packages that still expose `Install EcoreX WebUI.command`.

### Version Metadata

- `cli/VERSION` is bumped to `0.1.13`.
- `desktop/package.json` and `desktop/package-lock.json` are bumped to `0.1.13`.
- Desktop enterprise policy, WebUI bridge client key/app version, Admin API version, packaging scripts, install/check scripts, release workflow defaults, and release validator default version are bumped to v0.1.13.
- Admin API keeps v0.1.10-v0.1.12 client keys as rollout compatibility keys and adds `ecorex-desktop-v0.1.13` / `ecorex-web-v0.1.13-web.1`.
- `deploy/ecorex-site/manifest.json` is intentionally not bumped in this source pass. It must remain v0.1.12 until real v0.1.13 release artifacts exist.

### Windows Signing Boundary

- Certum/SimplySign private key containers can be visible only from an elevated administrator process.
- A normal PowerShell may show the certificate with `HasPrivateKey=True` while signing preflight still fails with `SimplySign CSP key containers: none visible`.
- Run `npm run sign:win:preflight` and the final `package:win:signed` flow from an elevated/admin shell after unlocking SimplySign/proCertum.

## Release Update Policy

- Windows: signed builds may be pushed as one-click updates after the current v0.1.13 NSIS installer is signed and smoke-tested.
- macOS: the client should prompt users to download the latest DMG from the download page; do not claim notarization unless the DMG is notarized.
- WebUI: after an update, reopening the app should show the user-facing update notes once per version.

## Validation Notes

Required before a v0.1.13 release candidate:

- Run backend tests that cover version notes, `find`, `ecorex_cli`, filesystem profiles, WebUI session handling, and SSE behavior.
- Run desktop typecheck.
- Rebuild the WebUI static bundle and verify the recorded static hash. The current v0.1.13 source-pass hash is `index-CcCofcc7.js`; the v0.1.12 hash `index-B_LYG2V7.js` is stale after this frontend change.
- Re-stage desktop runtime and validate the packaged runtime, not only source.
- Build/sign the Windows installer and verify Authenticode signature plus SHA256.
- Build macOS DMGs from the macOS workflow and clearly record whether they are notarized.
- Update `deploy/ecorex-site/manifest.json` only after every ready artifact has real size and SHA256 values.
- Run `scripts/validate-ecorex-release-artifacts.py --version 0.1.13` against the final public zip and desktop runtime.

## Current Release Boundary

This log records source changes for v0.1.13. It does not mark a public release complete. Any existing v0.1.12 installer, DMG, WebUI package, or public zip is stale for v0.1.13 and must not be renamed or reused as a new version.

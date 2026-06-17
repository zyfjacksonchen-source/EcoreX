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

## Release Candidate Update

- Rebuilt and validated the v0.1.13 WebUI packages after replacing the macOS `.command` launcher with `Install EcoreX WebUI.app`. The validator now rejects macOS WebUI packages that expose the Agent CLI/runtime terminal through `Install EcoreX WebUI.command`.
- Rebuilt the Linux/Web service package and validated the current renderer hash: `index-CcCofcc7.js` plus `index-D7oCsug3.css`.
- Rebuilt and signed the Windows NSIS setup after a direct elevated `signtool` retry. The final `desktop/release/EcoreX_0.1.13_x64-setup.exe` is Authenticode `Valid`, size `149193112`, SHA256 `D44E562E9874CAF7E9F2519FCDDE8A9EAC6A8E4D401956AB9672B4A051D4634B`.
- Added external release-artifact support as a fallback for large release payloads. The final v0.1.13 publication embeds the SHA256-verified macOS DMGs under `site/downloads/` so the public download page does not depend on private GitHub Release asset URLs.
- Re-ran the Build macOS Apps path on GitHub Actions `macos-15` with workflow_dispatch run `27604509625`, `mac_arch=all`, `notarize=false`, and `release_tag=v0.1.13`. Both `macOS DMG (arm64)` and `macOS DMG (x64)` completed successfully.
- Latest macOS desktop DMGs are intentionally unsigned/unnotarized but complete installable DMG outputs:
  - `EcoreX_0.1.13_arm64.dmg`, size `192665001`, SHA256 `EE1826474FBC99D0D54FF7FD09923BF82042C7816E9EE1864DD9532AFCD8549A`
  - `EcoreX_0.1.13_x64.dmg`, size `200043508`, SHA256 `517029EC4E716A92FF0F3BE98095AE2B892BF763070A4582520692551D114B86`
- `deploy/ecorex-site/manifest.json` is now v0.1.13 and marks the Windows desktop installer as `ready`; macOS DMGs are `ready-unsigned` and served from the public download host.
- Final generated public release zip is `release-artifacts/EcoreX_0.1.13-public-release.zip`, size `694958669`, SHA256 `F1DF81E07945AC5EFA1F2FAEB2C2A49C33F7D186FDC1EA04F640906A06CE0305`. Full validator passed with `--desktop-dir desktop\release\win-unpacked`.

## Post-RC Hotfix: Runtime Ready, Hidden Context, macOS WebUI Zip

- User-reported desktop error: `Error invoking remote method 'ecorex:sidecar-json': TypeError: fetch failed`.
- Root cause: the Electron sidecar marked the runtime `running` as soon as the Python child process spawned. In v0.1.13 more built-in abilities and warmups made the window between process spawn and HTTP `/api/version` readiness longer, so renderer API calls could hit a port that was not listening yet.
- Fix: Electron now treats spawn as `starting`, polls `http://127.0.0.1:<port>/api/version`, and only marks `running` after the local Web API is reachable. `fetchSidecarJson` waits for readiness and returns a structured local-runtime error instead of throwing the raw IPC/fetch exception.
- Startup mitigation: MCP and scheduler/evolution warmups are now launched after channels start, in a daemon `agent-runtime-warmup` thread. This keeps v0.1.13 abilities enabled but lets `/api/version` become available before heavy optional warmups finish.
- User-reported context leak: project prompt text (`【EcoreX 项目上下文】`, project path, project memory path, and prior user request wording) appeared as a visible user chat bubble.
- Root cause: Desktop appended project context directly into the `/message.message` field, so the conversation store persisted it as ordinary user text.
- Fix: Desktop sends the visible user request separately from `hidden_context`; WebChannel combines hidden context only for the agent input and stores `visible_message` for history. Conversation history display also strips legacy project-context messages that were already persisted by older v0.1.13 builds.
- User-reported macOS WebUI package issue: the public macOS WebUI download was a `.tar.gz`; after extraction the visible `.app` launcher was only about 2 KB and was not self-contained.
- Fix: the standalone macOS WebUI artifact is now `EcoreX_0.1.13-webui-macos-universal.zip` containing a self-contained `Install EcoreX WebUI.app`. The app includes `Contents/Resources/package/runtime`, `python`, `wheelhouse`, and `scripts`, so users can unzip and double-click the app without managing sibling package directories.
- Follow-up macos-15 smoke caught a launch cwd bug: the installer wrote `runtime/config.json` with the selected local port, but launched `app.py` from the wrapper cwd, so runtime fell back to `config-template.json` and port `9899`. The macOS installer now starts `app.py` from the installed `runtime` directory with `PYTHONPATH` set to that runtime.
- GitHub Actions macos-15 WebUI install smoke run `27614943747` passed after the cwd fix. It authenticated to the private `v0.1.13` Release asset, extracted the ZIP, rejected `.command` launchers, ran `Install EcoreX WebUI.app`, captured the browser open URL, and confirmed `/app/` responded.
- WebUI package hashes after this hotfix:
  - `EcoreX_0.1.13-webui-win-mac.zip`, size `238356152`, SHA256 `7CFA6F123F96CCF94E2565E9CCA4F2CBE5871E1F054ECEB20265E4B9A3FD5AD4`
  - `EcoreX_0.1.13-webui-windows-x64.zip`, size `72884909`, SHA256 `88F37BCD6C65C194398FF2248837F3BB0D3D6AE095859929558A813EFB40C61F`
  - `EcoreX_0.1.13-webui-macos-universal.zip`, size `165308769`, SHA256 `3E4EA259222133E4AA7B08B99CFB4FC2E016B29612DC7C1488D0CC7E228DD3AE`
- Validation passed for this source state: `npm run typecheck`, `python -m py_compile app.py channel\web\web_channel.py bridge\agent_bridge.py agent\memory\conversation_store.py scripts\validate-ecorex-release-artifacts.py`, and targeted zip structure checks for the self-contained macOS WebUI app plus Playwright wheels.
- Staleness boundary: the previously recorded signed Windows setup, macOS DMGs, and public release zip are stale after this hotfix. Rebuild/sign Windows, rebuild macOS DMGs via macos-15, regenerate public zip, run full validator, smoke, and redeploy before calling v0.1.13 final.

## Post-Hand-Test Hotfix: On-Demand Abilities And Identity Boundary

- User-reported latest desktop hand-test symptom: the app could remain on "waiting for runtime" after v0.1.13 enabled several heavy abilities.
- Root cause: desktop/WebUI runtime defaults still auto-started or auto-installed heavy optional capabilities at boot:
  - `chrome-devtools` MCP through `npx chrome-devtools-mcp@latest`
  - Browser CDP auto-launch
  - Feishu/Lark CLI auto-install
  - Scheduler background service
  - Self-evolution idle trigger
- Fix: these abilities are now discoverable but disabled by default across source config, Electron sidecar defaults, runtime warmup, AgentBridge, ToolManager, BrowserService, FeishuCli, and Windows/macOS WebUI local package config generation.
- New built-in agent tool: `optional_abilities`.
  - `list`/`status` are read-only and let the agent discover built-in, optional, and planned capabilities.
  - `enable`, `disable`, and `install` go through the shared tool permission broker before changing config or running capability installers.
  - The tool can enable `chrome-devtools-mcp`, `browser-cdp`, `feishu-cli`, `scheduler`, and `self-evolution`, and can install existing capability packs such as `browser-automation`, `office-pdf`, `memory-heavy`, `model-connectors`, `voice`, and `im-channels`.
  - `subagents` and `goals` remain documented planned capabilities, not silently enabled runtime features.
- Root-cause identity hardening:
  - Added `common/ecorex_identity.py` to sanitize assistant-visible output and persisted assistant messages from legacy product self-names to `EcoreX`.
  - MCP client handshake now identifies as `EcoreX` `0.1.13`.
  - `ecorex_cli`, host diagnostics, project hidden context, and Electron persona migration no longer prompt the model with legacy self-name instructions.
  - Electron sidecar migrates local `character_desc` if it still contains legacy product self-names.
- Validation after this source change:
  - `python -m py_compile app.py config.py bridge\agent_bridge.py bridge\agent_initializer.py agent\protocol\agent_stream.py agent\tools\optional_abilities\optional_abilities.py agent\tools\__init__.py agent\tools\tool_manager.py agent\tools\browser\browser_service.py agent\tools\feishu_cli\feishu_cli.py agent\tools\host_diagnostics\host_diagnostics.py agent\tools\mcp\mcp_client.py common\ecorex_identity.py common\ecorex_tool_permissions.py scripts\validate-ecorex-release-artifacts.py`
  - `optional_abilities list` returned 17 abilities with `chrome-devtools-mcp.enabled=false` and did not start MCP.
  - `npm run typecheck` passed in `desktop/`.
- Staleness boundary: all previously recorded v0.1.13 Windows installers, macOS DMGs, WebUI ZIPs, and public release ZIPs are stale after this hotfix. The next step is to rebuild and open the local desktop release for user hand-test only. Do not upload, deploy the download page, or push Git until the user confirms the new hand-test build.
- Local hand-test desktop build opened from `desktop/release/win-unpacked/EcoreX.exe`.
  - Renderer assets: `index-DGSZjdR_.js`, `index-D7oCsug3.css`.
  - `EcoreX.exe` is intentionally unsigned for hand-test only, size `210896896`, SHA256 `6FA52E0D912C25C5B61D1BE2FB0051AA377DAC2A82D45ABCBEDE300C0E65A829`.
  - Runtime `/api/version` responded with `0.1.13`.
  - Packaged runtime config has `self_evolution_enabled=false`, `scheduler_enabled=false`, `mcp_auto_start=false`, browser `cdp_auto_launch=false`, and Feishu `auto_install=false`.
  - Runtime log shows `MCP warmup skipped`, `Scheduler warmup skipped`, and `MCP auto-start disabled`.
  - A stale `chrome-devtools-mcp` process tree from an older WebUI runtime dated 2026-06-16 was identified and stopped; no non-PowerShell `chrome-devtools-mcp` process remained after cleanup.
- User-reported hand-test error after the rebuild: `Agent error: cannot access local variable 'conf' where it is not associated with a value`.
  - Root cause: `AgentBridge.agent_reply()` still had a branch-local `from config import conf` inside the scheduler context branch. Python treated `conf` as a local variable for the whole function, so normal non-scheduler chats reached the self-evolution gate with `conf` unbound.
  - Fix: remove the branch-local import and use the module-level `conf` imported at file load.
  - Validation: `python -m py_compile bridge\agent_bridge.py` passed, the local directory build was relaunched, `/message` smoke on `127.0.0.1:9899` returned SSE `done` with content `OK`, and no `Agent error` event was emitted.
- Post-fix regression evidence:
  - `python tests\test_ecorex_web_parallel_backend.py` passed `82` tests after the `conf` scope hotfix.
  - A temporary `/message` smoke with a unique `hidden_context` marker stored only the visible user message in `/api/history`; the hidden marker was absent from persisted history and the temporary session delete returned success.
  - `deploy/ecorex-site/manifest.json` remains in the guarded v0.1.13 post-hotfix state: visible artifacts are `pending-signature` or `pending-validation`, hidden archives are `archived-stale`, and the notes block says artifacts are disabled until rebuild/sign/smoke/user approval.

## Final v0.1.13 Release Pass

- User confirmed the final local hand-test build and approved signing, packaging, GitHub push, and production deployment.
- GitHub Desktop setup was corrected for future interactive source management:
  - `origin` now points to `https://github.com/zhangyifanjackson-dotcom/EcoreX.git`.
  - The previous CowAgent remote is preserved as `cowagent-origin`.
  - The existing `ecorex` remote is retained for explicit release scripts and command-line pushes.
  - The current branch tracks `origin/codex/ecorex-v0.1.13` so GitHub Desktop does not confuse the feature branch with `main`.
- Rebuilt, signed, and verified the final Windows installer:
  - `desktop/release/EcoreX_0.1.13_x64-setup.exe`
  - Authenticode `Valid`
  - size `149139328`
  - SHA256 `85D08C28094A9818052E9156FAD81BA5E46DFD2551AB06043B5B3E0B90F53865`
- Built final WebUI and Web service packages from the same runtime:
  - Windows WebUI ZIP size `72892147`, SHA256 `6C4AACE07BD7B9F7ED4F9A7BB4EE7CDF8E46E573F0AAA6DF6201839101E60705`
  - macOS WebUI ZIP size `165315881`, SHA256 `AA1CBC3EFE876B79D872DCA73AC5926E6FE48B7EEB136914BA130839D2EFB874`
  - Linux Web service tarball size `3130894`, SHA256 `D3515AFE57407052E39D3151B9408BD39F3EB6CE65429452C3D57BDF70D27C96`
- Re-ran Build macOS Apps through GitHub Actions `macos-15` workflow_dispatch run `27662225103`.
  - `EcoreX_0.1.13_arm64.dmg` size `192668572`, SHA256 `544E6385096821A993150A403199ADBE76C678C7AFF8BC9074E7F2BB28FFCE7E`
  - `EcoreX_0.1.13_x64.dmg` size `200046801`, SHA256 `B5BF52A5869C2EFABB2EB179D7984BE003A98DE224F397B696E86D897DBB5FAE`
  - Both DMGs are unsigned/unnotarized by release decision and are not renamed older artifacts.
- Regenerated and deployed `EcoreX_0.1.13-public-release.zip`, size `392424725`, SHA256 `FFC42E95DD38DDFE99249C126943946F19931F24552F5779E35520371E5DB517`.
- Production download page now serves manifest `0.1.13`; visible artifacts include per-artifact `version: 0.1.13`.
- Public HEAD checks passed for the download root, `/app/`, Windows installer, Windows WebUI ZIP, macOS WebUI ZIP, and both macOS desktop DMGs.
- Production Web runtime was upgraded from v0.1.12 to `/opt/ecorex-web/releases/20260617042234-v0.1.13`.
  - `ecorex-web.service` is active.
  - The release check passed using the actual local bind `BASE_URL=http://172.18.0.1:9909`.
  - Auth login, `/app/`, `/auth/check`, `/api/version`, and SSE health checks passed.
- Final local validator passed:
  - `python scripts\validate-ecorex-release-artifacts.py --manifest deploy\ecorex-site\manifest.json --artifact-dir release-artifacts --version 0.1.13 --public-zip release-artifacts\EcoreX_0.1.13-public-release.zip --desktop-dir desktop\release\win-unpacked`

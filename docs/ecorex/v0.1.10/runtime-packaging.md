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
- Windows staging writes this JSON as UTF-8 without BOM. Electron policy readers strip a BOM before parsing so admin override files do not break enterprise login.
- Default public policy:
  - `adminEventsUrl`: `https://www.ecoreai.cn/ecorex-agent/client/events`
  - `modelConfigUrl`: `https://www.ecoreai.cn/ecorex-agent/client/model-config`
  - `capabilityPolicyUrl`: `https://www.ecoreai.cn/ecorex-agent/client/capability-policy`
  - `clientEventKey`: `ecorex-desktop-v0.1.10`
- The default client event key is a public desktop channel marker, not a secret. It only opens login/policy transport routes; model credentials still require a valid enterprise user token.
- Policy loading now rejects unusable override files that do not contain a client key plus at least one policy URL, then falls back to the packaged default. This prevents stale empty userData policies from breaking first-run enterprise login.
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

## Desktop Runtime Defaults

- Before the sidecar starts, Electron ensures desktop-safe defaults in `config.json` when needed:
  - `channel_type`: `web`
  - `agent`: `true`
  - `knowledge`: `true`
  - `self_evolution_enabled`: `true`
- This step does not embed or overwrite model API keys. Enterprise model policy remains the source of truth for `model`, `bot_type`, API key, and Base URL.
- Project sessions add the selected project directory as an attachment and instruct the agent to write summaries to that project's `.ecorex/project-memory.md`, keeping project memory separate from the original global memory entry.

## Model Routing

- Desktop renderer does not call the upstream model endpoint directly. It sends chat turns to the local sidecar through `POST /message`.
- The runtime builds the OpenAI-compatible request body from the conversation/session context automatically. Users and admins should configure provider, model, API key, and Base URL; they should not enter the full `/chat/completions` endpoint.
- For OpenAI-compatible providers, runtime appends `/chat/completions` to the configured Base URL.
- Current production upstream requires a versioned redacted Base URL ending in `/v1`, so the actual upstream route is `baseUrl + /chat/completions`.
- There is no upstream `/v1/message` route in this build. `/message` is only the local desktop-runtime route.
- Verification on 2026-06-12:
  - Direct `/v1/chat/completions` returned a standard choices response for `gpt-5.5`.
  - Direct root `/chat/completions` returned a non-standard/empty shape.
  - Production Admin DB global model `api_base` was updated to the redacted `/v1` OpenAI-compatible base URL.
  - Installed runtime loaded that Base URL and local `/message` plus `/poll` returned `EcoreX OK` when polling until `has_content=true`.

## Playwright Capability Evidence

- The original runtime already defines `browser-automation` in `capabilities.json`; this pass reused it instead of adding a second browser stack.
- Verification command installed the pack through `desktop/scripts/install-capability.py` using the staged runtime Python.
- Status file: `desktop/runtime/ecorex-runtime/capability-state/browser-automation.json` reports `state=installed`, `installed=true`, and no missing modules.
- Runtime smoke: `from playwright.sync_api import sync_playwright` launched Chromium headless and read the data URL title `EcoreX Playwright OK`.
- Because this changes the staged runtime, the Windows installer must be rebuilt/signed again before claiming the published installer includes the preinstalled Playwright pack.

## Release Metadata

- Version sources must be unified to `0.1.10`: desktop package files, manifest, release docs, and verification script defaults.
- Current published Windows installer in public manifest: `EcoreX_0.1.10_x64-setup.exe`, size `117,529,640` bytes, SHA256 `C90C944E09CD5BB629ED60EAB33792D7948F5BAFABD71402948478486EC79FA7`.
- Latest local signed hardening installer after enterprise-policy fallback fix: `EcoreX_0.1.10_x64-setup.exe`, size `117,535,440` bytes, SHA256 `238B4A306386F3AAB1D5D8F4FF6C468B68A92A26AF52067D883E6BD60662747C`. Regenerate and deploy the public release zip before changing the public manifest to this artifact.
- Windows installed smoke passed after the enterprise policy packaging fix, the usage/SSE fix, and the preload/BOM fix: install found, app started, sidecar ready, packaged policy no-BOM, and desktop bridge available.
- Note: the current recorded signed installer predates the post-handoff UX correction pass and the local Playwright preinstall verification. Rebuild/sign/install again before updating public artifact hashes.

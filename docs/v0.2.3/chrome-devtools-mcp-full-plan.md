# R23-02C Chrome DevTools MCP Full-Compatible Enablement

## Scope

Enable the upstream Chrome DevTools MCP server as EcoreX's advanced CDP companion while keeping first-party `browser` as the compact browser automation path:

- Use the existing `tools.browser.cdp_endpoint` (`http://127.0.0.1:9222`) so BrowserAutomationService and `chrome-devtools-mcp` attach to the same EcoreX-managed CDP instance.
- Keep `mcp_auto_start=false` at boot. The MCP server remains discoverable/optional and starts only through explicit enablement or tool loading, while BrowserAutomationService still owns CDP auto-launch on first browser use.
- Use `npx -y chrome-devtools-mcp@latest` to avoid first-run prompt stalls.
- Enable the full-compatible tool profile for the `--browserUrl` mode: page-id routing, DevTools targets, vision click tools, structured content, all pages, memory debugging, third-party tools, WebMCP discovery, usage-statistics off, CrUX off, and network-header redaction.
- Bundle upstream agent skills: `a11y-debugging`, `chrome-devtools`, `chrome-devtools-cli`, `debug-optimize-lcp`, `memory-leak-debugging`, and `troubleshooting`.

## Explicit Non-Defaults

- Do not enable `--slim`.
- Do not enable `--categoryExtensions` by default because upstream documents it as pipe-only for now and incompatible with `browserUrl`/`wsEndpoint` until a future Chrome release.
- Do not allow non-localhost CDP endpoints, `--chromeArg`, custom executable paths, or unknown args to start silently. Those require the normal permission path.
- Do not store browser cookies, tokens, network headers, full OCR text, or raw trace content in public runtime events.

## Implementation Contract

- `config.py`, `config-template.json`, `config.json`, and `desktop/electron/sidecar.ts` must write the same canonical args.
- `agent/tools/mcp/mcp_client.py` marks only the canonical local default as `trusted_default_chrome_devtools`.
- `common/ecorex_tool_permissions.py` allows silent startup only for the canonical local default and still blocks read-only mode.
- `agent/tools/optional_abilities/optional_abilities.py` upgrades existing old default args to the canonical full profile and reports `fullToolset`.
- Chrome DevTools MCP skills are stored under `skills/` so the existing skill loader can discover them without a second skill registry.

## Evidence

- Upstream README and server configuration: https://github.com/ChromeDevTools/chrome-devtools-mcp
- Upstream tool reference and flags: https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/tool-reference.md
- Chrome DevTools for agents overview: https://developer.chrome.com/docs/devtools/agents/get-started

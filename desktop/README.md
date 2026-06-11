# EcoreX Desktop

EcoreX Desktop is the Electron + React shell for the compatible agent runtime.

Current scope:

- Electron main process with a minimal Python sidecar lifecycle manager.
- Whitelisted IPC API bridge from the renderer to the local sidecar.
- React workbench using the Codex-style layout: left session/project sidebar, right chat workspace.
- Orange brand tokens with Light / Dark / System theme switching.
- Settings Center entry for Models, Channels, Skills, MCP, Permissions, Files, and Diagnostics.
- Click-to-open file preview drawer.
- Human-in-the-loop confirmation card pattern.
- Runtime snapshot loading for sessions, tools, skills, models, and version.
- Minimal chat send/stream loop via `/message` and `/stream`.

The agent core remains in the Python project root. The desktop shell starts `python app.py` from the repository root unless `ECOREX_SKIP_SIDECAR=1` is set.

## Commands

```powershell
npm install
npm run typecheck
npm run build
npm start
```

Packaging:

```powershell
npm run package:dir
npm run package:win
npm run package:mac
```

Windows signing uses the external signing toolchain provided on this workstation. The desktop client must not delete, modify, or manage certificates.

```powershell
npm run sign:win
```

The signing script defaults to:

- SimplySign shortcut: `C:\Users\user\Desktop\SimplySign Desktop.lnk`
- Signing tools: `C:\脚本签名工具`
- signtool: `C:\脚本签名工具\signtool.exe`

macOS packaging is configured to output DMG directly with `npm run package:mac`.

For UI-only desktop development:

```powershell
$env:ECOREX_SKIP_SIDECAR='1'
npm run dev
```

Useful environment variables:

- `ECOREX_SKIP_SIDECAR=1`: skip Python sidecar startup.
- `ECOREX_PYTHON=C:\path\to\python.exe`: choose the Python runtime.
- `ECOREX_WEB_PORT=9899`: choose the local EcoreX runtime web port.
- `ECOREX_REPO_ROOT=C:\path\to\runtime`: override the compatible runtime repository root used by the sidecar.

## Verification

Current verified checks:

- `npm run typecheck`
- `npm run build`
- `npm audit --audit-level=critical`
- `python -m py_compile channel\web\web_channel.py`
- Static check: no color literals outside `desktop/src/styles/tokens.css`

Browser screenshot verification is still pending because the in-app browser control tool was not available in this run.

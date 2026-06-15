# EcoreX v0.1.12 Release Manifest

Date: 2026-06-15

## 2026-06-15 Public Release Update

- Public download page is live at `https://www.ecoreai.cn/ecorex-agent/`.
- Windows desktop installer is signed and published:
  - `EcoreX_0.1.12_x64-setup.exe`
  - size `117,665,632`
  - SHA256 `626C5FA5C16F1C0EFB4DB884C57FE186D1310DF5D635E0D0FE836294F8472271`
- Local WebUI is published as one dual-platform package:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `99,710,949`
  - SHA256 `8E8DA035A72EFC4DF7CB9C12A311355189743D85B83596C59611D08C8976578D`
- Hidden compatibility packages remain publishable:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,315,397`
  - SHA256 `F1DABD1A5A839EE2B9593C9178EDB3D303A5CB996AF8147714C6AA8984055206`
- Public release zip:
  - `EcoreX_0.1.12-public-release.zip`
  - SHA256 `0A4E5AD60E23D8313811A5E2A2FC18A685C8ABED74593F827CDD3AEC36400988`
- WebUI `/chat` now serves the same desktop-style React app as `/app/`.
- Local WebUI proxies `/client/*` to the Admin API client endpoints and no longer relies on a local fallback account once the backend is reachable.
- Packaging and release pitfalls from this rollout are captured in `docs/ecorex/packaging-release-guidelines.md`.

## Scope

- Desktop UX fixes: session switching clears stale transcript/composer state immediately; model selector shows the active model and no longer opens Settings; session pin/rename/delete actions are hover-only; stop/cancel can close an in-flight assistant bubble even before a backend request id arrives; failed tool details stay collapsed by default; dark mode now updates the Windows title bar; the jump-to-latest control is centered/larger; expanded long answers place the collapse button below the full reply.
- Composer telemetry: the composer now shows compact daily and weekly token usage meters, plus an estimated current-context meter under the input. The default context threshold is `258k`; compacting still follows the existing threshold logic.
- Browser automation: CDP is the first browser automation path, auto-launching Chrome/Edge on `http://127.0.0.1:9222` with Playwright fallback. The packaged default also registers `chrome-devtools-mcp@latest --autoConnect`.
- Browser loop fix: browser click/wait/press tool results now include a bounded post-action snapshot so the agent can observe refreshed pages instead of staying in a "thinking" loop after the page already answered.
- Admin console: token totals and limits use compact `k/m` labels with exact values in hover titles; usage statistics only include active, non-deleted EcoreX users; the error traceback view defaults to failures only and no longer stores warn/info/success client events in `error_logs`; the sidebar brand mark no longer depends on a possibly missing image.

## Version Sources

- Desktop package: `desktop/package.json` and `desktop/package-lock.json` are `0.1.12`.
- Admin API: `deploy/ecorex-admin-api/ecorex_admin_api.py` reports `0.1.12`.
- Desktop enterprise policy default client event key: `ecorex-desktop-v0.1.12`.
- Download page production deployment is active for this round.

## Windows Artifact

- Artifact: `desktop/release/EcoreX_0.1.12_x64-setup.exe`
- Size: `117,665,632` bytes
- SHA256: `626C5FA5C16F1C0EFB4DB884C57FE186D1310DF5D635E0D0FE836294F8472271`
- Blockmap: `desktop/release/EcoreX_0.1.12_x64-setup.exe.blockmap`
- Blockmap size: `172,970` bytes
- Blockmap SHA256: `9C740E5C38397099492CD554EB273C6F5A147EF071A7B6E3DA53D2998FA87746`
- Blockmap note: this blockmap predates the signed EXE and must not be used for auto-update until regenerated from the signed installer.
- Authenticode status: `Valid`
- Build note: packaged from the short `X:` subst path to avoid Windows long-path failures during runtime staging. Runtime staging reused the locally installed EcoreX Python runtime with `-SkipDependencyInstall`, while source/config files were copied fresh from this checkout.
- GitHub source commit: `b3d1bbf292f00c775e051ea6f5e0657c06cc0957` initially published v0.1.12. A follow-up v0.1.12 hardening sync updates the same tag/release assets after the sub-agent review fixes.
- GitHub Release: `https://github.com/zhangyifanjackson-dotcom/EcoreX/releases/tag/v0.1.12`
- Note: the repository/release may require an authenticated GitHub session; anonymous API checks can return 404 if the repo is private.

## Verification

- `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py`
- `node --check deploy/ecorex-site/admin/admin.js`
- `npm run typecheck`
- `npm run build`
- Runtime staging: `scripts/stage-runtime-win.ps1 -PythonHome <installed EcoreX runtime python> -SkipDependencyInstall`
- Packaging: `electron-builder --win --publish never` with Electron/electron-builder downloads served from npmmirror after GitHub download timeout.
- Artifact signature check confirms the installer is signed.

## Deployment Boundary

- Deploy the Admin Web/API as v0.1.12 first.
- `/srv/ecorex-agent-download/current` now points to the v0.1.12 public release.
- GitHub source sync and release upload should include this manifest and the packaging guidelines so a fresh clone can resume from the same state.

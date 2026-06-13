# EcoreX v0.1.12 Release Manifest

Date: 2026-06-13

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
- Download page production deployment is intentionally deferred for this round.

## Windows Artifact

- Artifact: `desktop/release/EcoreX_0.1.12_x64-setup.exe`
- Size: `165,733,775` bytes
- SHA256: `90A215B05390183D3FC3169F5DFB0110AD37BFF6D17A24F81FBDE6AFCB2905BA`
- Blockmap: `desktop/release/EcoreX_0.1.12_x64-setup.exe.blockmap`
- Blockmap size: `172,970` bytes
- Blockmap SHA256: `9C740E5C38397099492CD554EB273C6F5A147EF071A7B6E3DA53D2998FA87746`
- Authenticode status: `NotSigned`
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
- Artifact signature check confirms the installer is unsigned.

## Deployment Boundary

- Deploy the Admin Web/API as v0.1.12 first.
- Do not advance `/srv/ecorex-agent-download/current` or the public download page to v0.1.12 until the user explicitly asks for the download page rollout.
- GitHub source sync and release upload should include this manifest so a fresh clone can resume from the same state.

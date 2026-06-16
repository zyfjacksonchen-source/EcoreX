# EcoreX v0.1.12 Hardening Development Log

Date: 2026-06-15

This log is the continuity record for the post-release v0.1.12 hardening goal.
Keep it updated before build/package/upload work so future turns do not lose the
current investigation state.

## 2026-06-16 Published Release Record

- Shared `MessageContent` now recognizes and makes clickable `http://`,
  `https://`, `file://`, Windows drive paths, UNC paths, and common absolute
  Unix paths.
- Local path opening is host-specific: Desktop uses Electron `openPath` for
  local filesystem targets, while WebUI opens local files through `/api/file`.
- Static WebUI assets were rebuilt and synced to `index-B_LYG2V7.js`.
- Windows v0.1.12 setup was signed with SimplySign through elevated `signtool`
  and published.
- WebUI Windows and macOS packages were built and published.
- Production Web runtime was deployed, including the Node/npx fix required for
  `chrome-devtools` MCP/CDP startup.
- macOS Desktop DMGs were built on GitHub Actions `macos-15` via
  workflow_dispatch after the workflow was changed to publish directly to
  GitHub Release `v0.1.12` instead of `actions/upload-artifact`, which was
  blocked by artifact storage quota recalculation.
- Final production download page now serves the signed Windows installer, the
  macOS Apple Silicon and Intel desktop DMGs, and separate Windows/macOS WebUI
  packages. The final public release zip is size `694,836,215`, SHA256
  `230B028E70A5B8E6CDF39F1D14CB7ACC752D9A1D7DA1E0E2544975A345B6FC70`.

## 2026-06-16 Rebuild 0023 Current Candidate

- Rebuilt current hand-test candidate as `release-local-0023` after the
  managed skill refresh, OpenAI image edit/generation endpoint audit, and
  admin image model catalog/hint hardening. `release-local-0022` is stale.
- Root cause found during local WebUI image-generation smoke: EcoreX was
  loading `~/EcoreX/skills/image-generation`, an older workspace copy from a
  previous release, instead of the packaged 6/16 built-in skill. That stale
  copy defaulted to `gpt-image-2` and masked the new `gpt-image-2-pro` logic.
- Fixed source:
  - `agent/skills/manager.py` refreshes allowlisted official built-in skills
    from the packaged `skills/` directory when a workspace copy misses
    release-critical markers. Existing explicit user overrides are preserved
    by `.ecorex-custom-override`.
  - `agent/skills/service.py` writes `.ecorex-custom-override` when a user or
    admin explicitly installs a same-name built-in override with
    `allow_builtin_override=true`.
  - `skills/image-generation/SKILL.md` now states that OpenAI requests without
    input images use `/images/generations`, while edit/reference/local input
    image or `image_url` requests use `/images/edits` with multipart `image` /
    `image[]`.
  - `scripts/validate-ecorex-release-artifacts.py` now requires the managed
    skill refresh sentinels plus both `/images/generations` and
    `/images/edits` in the packaged OpenAI image-generation skill.
  - `channel/web/web_channel.py` now exposes `gpt-image-2-pro` first in the
    Admin/Models image catalog and auto hint, so the UI matches runtime
    behavior.
- Validation:
  - OpenAI official docs were checked for Image API generation and edit
    parameters. Public docs list `gpt-image-2`; the user has already verified
    the configured backend accepts `gpt-image-2-pro`, so EcoreX keeps the
    requested `gpt-image-2-pro` default and falls back to `gpt-image-2` only on
    model/access unavailability.
  - `python -m unittest tests.test_ecorex_web_parallel_backend` passed
    (`79` tests). New coverage asserts no-image OpenAI requests use
    `/images/generations`, single-image edit requests use multipart `image`,
    multi-image edit requests use multipart `image[]`, and Admin image auto
    hints choose `gpt-image-2-pro`.
  - The local workspace copies under `C:\Users\user\EcoreX\skills` were
    refreshed once, proving the migration path updates stale built-in copies.
- Packaging/build evidence:
  - Desktop: `desktop/release-local-0023/win-unpacked/EcoreX.exe`.
  - Windows WebUI: SHA256
    `2168D6F826221DBCD94BDC8F1F8CBC9C4E642A039C846E88E7C444866E9A19F2`,
    size `72,875,304`.
  - macOS WebUI: SHA256
    `CF3B4099B9B7425BA5A8EC976988BF0010E5A8BB75636B03B0AA90B81138AC24`,
    size `79,798,534`.
  - Archived dual WebUI: SHA256
    `358CF5C799206EC6954291CA2B3E76F28E5708E8B6AE2FB79090A69BF173F357`,
    size `153,024,284`.
  - Archived Linux service tarball: SHA256
    `3323450FE5C4B0FBA117BE2E4717155FFA7AA6570EDBF320F54D2DDCA4E15592`,
    size `3,120,581`.
  - Public release ZIP: SHA256
    `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`,
    size `154,772,792`.
- Runtime smoke:
  - Desktop `http://127.0.0.1:9899/app/` and WebUI
    `http://127.0.0.1:9909/app/` run simultaneously.
  - Both serve `index-CjBkNLMl.js` / `index-BG_69rJD.css`, expose
    `feishu_cli`, `host_diagnostics`, `browser`, and `bash`, return empty
    active-request snapshots, and round-trip permission mode
    `read-only -> full-access`.
  - Desktop packaged runtime, installed WebUI runtime, and workspace
    `image-generation` all default to `gpt-image-2-pro`, route no-input-image
    calls to `/images/generations`, route edit/reference/local input image or
    `image_url` calls to `/images/edits` multipart `image` / `image[]`, and omit
    `response_format` in the OpenAI branch.
  - Real installed-WebUI image smoke using the admin-configured OpenAI-compatible
    backend passed with explicit `model="gpt-image-2-pro"`: elapsed `22.74s`,
    reported model `gpt-image-2-pro`, output PNG
    `C:\CowAgent\release-artifacts\image-smoke-0022-explicit-pro\09a2a7232e4e.png`
    (`768,607` bytes). Credentials were only injected into the child process
    environment and were not printed.
  - Download page browser render smoke passed after updating the cache-buster to
    `site.js?v=0.1.12-0023`: four cards rendered, required PNG assets decoded
    in-browser, no broken images were detected, ready links were limited to the
    Windows/macOS WebUI artifacts, and desktop cards stayed disabled as pending
    signing/verification.

## 2026-06-16 Rebuild 0021 Source Delta

- Parallel review found the most important remaining boundary bug: the
  no-profile filesystem fallback included `web_file_serve_root`, whose default
  was Home. This made default reads broader than the intended workspace/cwd
  boundary.
- Fixed source:
  - `common/ecorex_tool_permissions.py` default profile now uses
    `agent_workspace`/cwd only.
  - `channel/web/web_channel.py` `/api/file` defaults to workspace/upload roots
    and log streaming uses permission-checked, masked host-diagnostics tails.
  - `agent/memory/manager.py`, `agent/memory/service.py`, and
    `agent/tools/memory/memory_get.py` enforce filesystem profile reads.
  - `agent/protocol/agent_stream.py` forces the next turn text-only when
    `feishu_cli` reports `authRequired=true` or `available=false`.
  - `skills/create-xiaohongshu-note/references/feishu-reference-library.md`
    now describes a finite setup/auth flow instead of "try until reading
    works".
  - OpenAI image generation defaults to `gpt-image-2-pro`, uses official GPT
    Image parameters, and falls back to `gpt-image-2` only on model/access
    unavailability. The local connectivity smoke currently stops at
    `missing_key` because this machine has no `OPENAI_API_KEY`.
  - Download page UTF-8 text, final grid grouping, image preloads/fallbacks, and
    validator image-asset checks were restored.
- Validation before rebuild:
  - `python -m unittest tests.test_ecorex_web_parallel_backend` passed
    (`74` tests).
  - Changed runtime and validator `py_compile` passed.
  - `node --check deploy\ecorex-site\site.js` passed.
  - Download page assets decoded successfully with PIL.
- Release state:
  - `release-local-0020` and all 0020 WebUI/public artifacts were stale.
  - This pass was rebuilt as `release-local-0021`, but 0021 became stale after
    the managed skill refresh and image edit/generation endpoint audit.
  - Final public download grid should publish only Windows desktop, macOS DMG
    choices, Windows WebUI, and macOS WebUI. The dual Win/Mac WebUI package and
    Linux service package are archived/hidden unless the release plan changes.

## 2026-06-16 Agent Host Boundary Rebuild 0020

- Followed up the Agent Core/Codex-boundary audit after `release-local-0018`.
  The conclusion remains precise: EcoreX is a broker/profile and
  tool-routing boundary, not an OS-level Codex sandbox. The product wording
  must remain `Codex-boundary-inspired`.
- New source fixes:
  - `agent/memory/summarizer.py` now asks the shared filesystem profile before
    background memory initialization or writes.
  - `agent/knowledge/service.py` now asks the same profile before knowledge
    list/read/graph reads.
  - `agent/tools/web_fetch/web_fetch.py`,
    `agent/tools/web_search/web_search.py`, and `agent/tools/vision/vision.py`
    now fail closed when the permission broker is unavailable.
  - `common/ecorex_tool_permissions.py` now falls back to
    `config.get_appdata_dir()/permissions` when no explicit
    `ECOREX_USER_DATA` or `ECOREX_DESKTOP_USER_DATA` is set. This fixed the
    WebUI package smoke finding where an extracted WebUI could otherwise share
    the global `%LOCALAPPDATA%\EcoreX\permissions` state instead of its own
    configured `appdata_dir`.
  - `scripts/validate-ecorex-release-artifacts.py` now requires packaged
    runtime sentinels for appdata-backed permission state, memory/knowledge
    hooks, and network/vision fail-closed behavior.
- Regression evidence:
  - `python -m py_compile common\ecorex_tool_permissions.py agent\memory\summarizer.py agent\knowledge\service.py agent\tools\web_fetch\web_fetch.py agent\tools\web_search\web_search.py agent\tools\vision\vision.py tests\test_ecorex_web_parallel_backend.py scripts\validate-ecorex-release-artifacts.py`
    passed.
  - Targeted permission appdata and permission API tests passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`64` tests).
  - `npm run typecheck`, `npm run build`, and `npm run stage:runtime:win`
    passed.
- Packaging/build evidence:
  - Electron Builder initially failed on remote GitHub/mirror downloads. The
    successful build used the local Electron distribution cache:
    `npx electron-builder --win --dir --publish never --config.directories.output=release-local-0020 --config.electronDist=node_modules/electron/dist --config.win.signAndEditExecutable=false`.
  - Desktop local hand-test package:
    `desktop/release-local-0020/win-unpacked/EcoreX.exe`.
  - WebUI dual ZIP SHA256:
    `89FA0E92BFEE3087545714EF8CE7583C45D12106C63C6CEAB482962F0118BFB9`.
  - Windows WebUI ZIP SHA256:
    `020D6F30101FB5304CD6BE4C92DB1BB2A2CDFB5B426FC3240877A3871B5A7C5A`.
  - macOS WebUI tarball SHA256:
    `294F94C9E1FFAE62845A2CC498C19595C076E7DBC7D48CAB12E6C13D8337AEEA`.
  - Linux/Web tarball SHA256:
    `10AB3F55BA5D60158EA4D1B2AE9B0D61CA1207630F5CA5C3986A4F1207D40927`.
  - Public release ZIP SHA256:
    `E1E90D76B94FE289140F7C2411CD55456A2FF71CD9C55B7909AAECE38BE04B28`.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0020\win-unpacked`
    passed.
- Runtime smoke:
  - Desktop `127.0.0.1:9946` and extracted WebUI `127.0.0.1:9947` ran
    simultaneously and returned `/api/version = 0.1.12`.
  - Both served `index-CjBkNLMl.js` and `index-BG_69rJD.css`, exposed
    `feishu_cli`, `host_diagnostics`, and `browser`, and returned
    `/api/active-requests`.
  - Packaged WebUI runtime, with no permission env override, resolved
    permission state to its configured appdata path and passed
    `read-only` write block plus `full-access` write allow smoke.
  - Packaged WebUI runtime custom filesystem smoke proved blocked memory paths
    do not create files, daily memory creation raises, secret knowledge files
    are blocked/hidden, and public knowledge remains readable.
  - Packaged WebUI CDP MCP startup reached `chrome-devtools` ready with 29
    namespaced tools.
- Manual test/publish state:
  - `release-local-0018` and `release-local-0019` were stale; for that pass,
    `release-local-0020` was the only valid hand-test artifact.
  - Upload/deploy/GitHub sync remains paused until user manual testing passes.
  - Windows signed setup remains blocked on the signing provider/private key.

## 2026-06-16 Background Memory And Knowledge Boundary Follow-Up

- Rechecked EcoreX's Agent Core and host boundary after the user asked whether
  EcoreX itself can align with Codex's capability boundary.
- Key conclusion: EcoreX is still a broker/profile boundary, not an OS-level
  Codex-equivalent sandbox. The correct claim remains
  `Codex-boundary-inspired` until durable process sessions, restart replay,
  patch/worktree transactions, product subagents, and full network/filesystem
  profiles exist.
- Closed newly found concrete bypasses:
  - Automatic memory persistence now calls the shared filesystem profile before
    creating/writing `MEMORY.md`, `memory/YYYY-MM-DD.md`, user memory files, or
    Deep Dream diary files.
  - Knowledge list/read/graph APIs now ask the same profile before reading
    `knowledge/*.md` files.
  - `web_fetch`, `web_search`, and `vision` now fail closed when the permission
    broker is unavailable instead of continuing with network/model upload work.
  - The release validator now requires packaged runtime sentinels for the
    memory, knowledge, web fetch/search, and vision boundary hooks.
- Regression evidence so far:
  - `python -m py_compile agent\memory\summarizer.py agent\knowledge\service.py agent\tools\web_fetch\web_fetch.py agent\tools\web_search\web_search.py agent\tools\vision\vision.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - Targeted host-boundary tests passed for custom-profile memory write blocks,
    custom-profile knowledge read blocks, and read-only network/vision blocks.
- Packaging state:
  - `release-local-0018` is stale after this source change.
  - Rebuild desktop/WebUI/Linux/public packages and rerun validator before any
    hand-test, GitHub sync, upload, or deployment.

## 2026-06-16 Filesystem Profile Follow-Up Rebuild 0018

- Parallel read-only review of the 0017 boundary work found three practical
  gaps:
  - `send` still only checked read-only and could bypass filesystem profile
    deny rules when sending a local file.
  - Desktop bridge allowlist did not include `GET /api/active-requests`, so the
    renderer's runtime snapshot could silently fall back to an empty active
    request list.
  - Electron `PermissionManager` cached `permissions.json`, so the Desktop UI
    could show stale mode/grant state after the Python broker wrote the same
    file.
- Fixed:
  - `agent/tools/send/send.py` now calls
    `authorize_file_access("read", absolute_path, cwd=self.cwd)` before it
    checks or returns local file metadata.
  - `desktop/electron/apiBridge.ts` now allows `GET /api/active-requests`.
  - `desktop/electron/permissions.ts` reloads `permissions.json` on each
    `loadSettings()` call instead of serving stale cached state.
  - `agent/tools/evolution_undo/evolution_undo.py` now fails closed if the
    permission broker is unavailable.
  - `scripts/validate-ecorex-release-artifacts.py` now requires packaged
    `send.py` and checks for the `authorize_file_access("read", ...)` hook.
- Regression evidence:
  - `python -m py_compile agent\tools\send\send.py agent\tools\evolution_undo\evolution_undo.py scripts\validate-ecorex-release-artifacts.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - Targeted host-boundary tests passed, including custom filesystem profile,
    `/api/file`, send/env_config read-only, and evolution_undo/web fetch/search
    read-only coverage.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`61` tests).
  - `npm run typecheck` and `npm run build` passed.
- Packaging/build evidence:
  - `npm run stage:runtime:win` passed.
  - Electron dir package produced
    `desktop/release-local-0018/win-unpacked/EcoreX.exe`.
  - WebUI dual Win/Mac, Windows WebUI, macOS WebUI, Linux/Web, and public
    release packages were regenerated.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0018\win-unpacked`
    passed.
- Artifact hashes for this superseded 0018 rebuild:
  - WebUI dual ZIP:
    `11F52C262AF951A1C710A8F6AC6B23CC81A106D4CDE332F03867F350DB2E001C`.
  - Windows WebUI ZIP:
    `CF0E70D419EACE4E452F8871C31B1708477F73A6F0522549F35809C1989CD92A`.
  - macOS WebUI tarball:
    `C59AA5E8DE5281CDE5043850FB98A6557E842D00581CD16D96BF2E1FCE42B5A1`.
  - Linux/Web tarball:
    `B89913E4DED205C6912C04A2A5172638736E86C2A5846C3BA7863171B669EFF1`.
  - Public release ZIP:
    `E3F38AF9898CC091055FB2E37422ACD772BE06AC041ECFA539A19181ECC36A76`.
- Runtime smoke:
  - Desktop `release-local-0018` ran on `127.0.0.1:9942`.
  - Freshly extracted WebUI dual package ran on `127.0.0.1:9943`.
  - Both returned `/api/version = 0.1.12`, served `index-CjBkNLMl.js`,
    exposed `feishu_cli`, `host_diagnostics`, and `browser`, returned
    `/api/active-requests`, and passed permission mode round-trip.
  - Packaged WebUI runtime custom profile smoke passed for
    `read/write/ls/edit/send`: workspace access allowed, `.env` and outside
    paths denied.
  - Running WebUI `/api/file` custom profile smoke returned `200` for an
    allowed workspace file and `404` for deny-glob `.env`.
- Manual test/publish state:
  - For that pass, `release-local-0018` and the hashes above were the next
    manual-test inputs. Anything through `release-local-0017` was stale.
  - Upload/deploy/GitHub sync remains paused until user manual testing passes.
  - Windows signed setup remains blocked on the signing provider/private key.

## 2026-06-16 Filesystem Profile Boundary Rebuild 0017

- Continued the Codex-boundary pass after the strict recheck found the largest
  remaining hard boundary gap was local file access. Implemented the first
  EcoreX filesystem permission profile layer:
  - `common/ecorex_tool_permissions.py` now exposes `authorize_file_access()`
    and `_evaluate_filesystem_profile()`.
  - `read`, `ls`, `write`, and `edit` call the broker for concrete
    read/write decisions.
  - `/api/file` keeps its authenticated/root-confined behavior and now also
    asks the same broker before serving a local file.
  - `custom` mode without a filesystem profile fails closed for local file
    access. Explicit profiles support workspace roots, `read`/`write`/`deny`,
    and deny globs such as `**/*.env`.
- Regression evidence:
  - `python -m py_compile common\ecorex_tool_permissions.py agent\tools\read\read.py agent\tools\ls\ls.py agent\tools\write\write.py agent\tools\edit\edit.py channel\web\web_channel.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`61` tests).
  - New host-boundary tests cover workspace-only custom profiles, deny globs,
    read/write/list/edit decisions, `/api/file` profile enforcement, and
    read-only write blocking.
- Packaging/build evidence:
  - `npm run typecheck`, `npm run build`, and `npm run stage:runtime:win`
    passed.
  - Electron dir package produced
    `desktop/release-local-0017/win-unpacked/EcoreX.exe`.
  - WebUI dual Win/Mac, Windows WebUI, macOS WebUI, Linux/Web, and public
    release packages were regenerated from the same runtime source.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0017\win-unpacked`
    passed. The validator now checks packaged runtime source sentinels for
    `authorize_file_access`, filesystem profile evaluation, file tool
    read/write hooks, and `/api/file` profile enforcement.
- Artifact hashes for this superseded 0017 rebuild:
  - WebUI dual ZIP:
    `0B99C7C0941D5C93F0825C6719098DF59043A4CCEDA76882D29869E8B405BE27`.
  - Windows WebUI ZIP:
    `C11A9280B78EDA1CD4BBFCCB30596E19CAFC3D1D15BC9B14477B362F87195B27`.
  - macOS WebUI tarball:
    `F3AEF9D3AD1FA522EA10F467ED985B3BDA15007526DAFC5924F8C4CA360DCFB2`.
  - Linux/Web tarball:
    `6A7FBB149C1FF617283F9593D96F52DFEBB58280F248F34E620BF6D3C63136DE`.
  - Public release ZIP:
    `4EDA56A2240691FAC5D7E9264886221BD5674EDA2F1989E934EED69F3161BFF3`.
- Runtime smoke:
  - Desktop `release-local-0017` ran on `127.0.0.1:9940`.
  - Freshly extracted WebUI dual package ran on `127.0.0.1:9941`.
  - Both returned `/api/version = 0.1.12`, served `index-CjBkNLMl.js`,
    exposed `feishu_cli`, `host_diagnostics`, and `browser`, returned
    `/api/active-requests`, and passed permission mode round-trip.
  - Packaged WebUI runtime file tools passed custom filesystem profile smoke:
    workspace read/write/ls/edit allowed, `.env` and outside paths denied.
  - Running WebUI `/api/file` passed the same profile boundary: an allowed
    workspace file returned `200`, while a deny-glob `.env` returned `404`.
- Boundary truth:
  - This closes the biggest file-tool mismatch from the strict Codex recheck,
    but EcoreX is still not Codex-equivalent. Remaining hard gaps are a
    user-facing filesystem profile editor/API, network profiles, durable
    process/turn sessions, replayable run logs across restart, patch/worktree
    transactions, and product-level sub-agent orchestration.
- Manual test/publish state:
  - Use `release-local-0017` and the new WebUI/public hashes for the next
    manual test. Anything through `release-local-0016` is stale.
  - Upload/deploy/GitHub sync remains paused until user manual testing passes.
  - Windows signed setup remains blocked on the signing provider/private key.

## 2026-06-16 Strict Agent Core Boundary Recheck

- Rechecked EcoreX Agent Core against current Codex boundary concepts from
  OpenAI Docs MCP after the direct Codex manual helper returned HTTP 403.
- Conclusion for that recheck was unchanged but sharper: EcoreX is aligned or close at
  approval-policy intent, CDP-first browser routing, Feishu routing,
  dangerous-tool gating, skill workspace-overlay repair, MCP error semantics,
  and same-process active-request/SSE recovery.
- EcoreX is still not Codex-equivalent because it does not yet have
  filesystem/network permission profiles, durable command/process sessions,
  patch/worktree transactions, product-level subagent orchestration, or
  replayable run/event state across runtime restart.
- Important code evidence:
  - `common/ecorex_tool_permissions.py` now gates high-risk tools, including
    file writes, skill writes, web tools, scheduler, send, vision, MCP, browser,
    Feishu, and shell.
  - `agent/tools/read/read.py` and `agent/tools/ls/ls.py` still allow absolute
    path read/list outside the workspace, with only narrow credential path
    blocks. This is the largest remaining mismatch with Codex filesystem
    permission profiles.
  - `channel/web/web_channel.py` `/api/file` is authenticated and root-confined,
    but it is not yet driven by the same agent filesystem profile.
  - `agent/tools/browser/browser_service.py` remains CDP-first via
    `connect_over_cdp`, with auto-launch for Chrome/Edge CDP.
  - MCP `tools/call` error propagation and `McpTool` failure semantics are now
    real failure signals.
- Next implementation target, if the user wants true Codex-like boundary
  behavior, is an EcoreX permission-profile layer for filesystem and network
  access. Without that, the product should continue to say
  "Codex-boundary-inspired" rather than "Codex-equivalent".

## 2026-06-16 Agent Core Host-Boundary Rebuild 0016

- Re-audited the runtime against the parallel explorer findings. Several
  older findings are already closed in code before this rebuild:
  - `bash` and `feishu_cli` are cancel-aware and kill subprocess trees.
  - MCP `isError` / JSON-RPC errors return `ToolResult.fail`.
  - Skill package install uses safe path validation and safe zip extraction.
  - Same-request SSE no longer uses a single shared consumer queue.
- Remaining Codex-boundary truth:
  - EcoreX aligns with Codex at policy, diagnostics, CDP-first routing,
    Feishu wrapper routing, permission mode enforcement, and current-process
    active request recovery.
  - EcoreX is still not Codex-equivalent because it lacks a durable app-server
    turn/process model, managed PTY/process sessions, `turn/steer`, product
    sub-agents, patch/worktree transactions, custom sandbox policy language,
    and replayable event logs across runtime restart.
  - Do not market or document this as "Codex-equivalent"; the correct release
    wording is "Codex-boundary-inspired hardening".
- Built then-current hand-test/package set:
  - Desktop: `desktop/release-local-0016/win-unpacked/EcoreX.exe`.
  - WebUI dual ZIP SHA256
    `16CAC376CDEA29BCAA73808A8CE404EF92017EBAD1981EA306A6CDDED336CA06`.
  - Windows WebUI ZIP SHA256
    `8BB114AED0A9D8AC6ADF827F67ACE3765CBE92AD85B20764F1CC4A35DCECCC44`.
  - macOS WebUI tarball SHA256
    `9E4DF4098BDEAF48C93D54949B473BEC971C90BEDF335B7086EC38A0F2AF6BE7`.
  - Linux/Web tarball SHA256
    `620704EAC2D2DA1DD7C57E6A6BEB003353AC811720C7246D6AA54A5993F9BBAB`.
  - Public release ZIP SHA256
    `D406F1C6ABB07A0EC9DBEE239D936BC4D5FDA48FCBD26DDB28CB4CE69D9E300B`.
- Validation passed:
  - `python -m py_compile channel\web\web_channel.py agent\protocol\agent_stream.py scripts\validate-ecorex-release-artifacts.py tests\test_ecorex_web_parallel_backend.py`.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`59` tests).
  - `npm run typecheck`.
  - `npm run build`.
  - `npm run stage:runtime:win`.
  - Electron dir package:
    `npx electron-builder --win --dir --publish never --config.directories.output=release-local-0016 --config.win.signAndEditExecutable=false`.
  - WebUI/Linux/public packaging scripts.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0016\win-unpacked`.
- Runtime smoke passed:
  - Desktop `127.0.0.1:9938` and installed WebUI `127.0.0.1:9939` ran
    simultaneously on one device.
  - Both returned `/api/version = 0.1.12`, served `index-CjBkNLMl.js`, exposed
    `feishu_cli`, `host_diagnostics`, and `browser`, returned
    `/api/active-requests`, and passed `read-only -> full-access` permission
    mode round-trip.
  - Packaged desktop and installed WebUI Python runtimes both passed same
    `request_id` dual-subscriber SSE broadcast smoke.
  - Packaged desktop and installed WebUI Python runtimes both passed raw
    `npx @larksuite/cli...` autoroute into `feishu_cli`. Desktop package also
    covered `node C:/cli-main/scripts/run.js ...` and `lark-cli auth login`.
- Browser visual/DOM smoke passed with packaged Python Playwright and local
  Edge:
  - Fresh unauthenticated headless browser loaded both desktop sidecar
    `9938/app/` and WebUI `9939/app/` without white screen, with
    `data-theme=dark`, `index-CjBkNLMl.js`, and `index-BG_69rJD.css`.
  - Logged-in session smoke loaded the main app for both surfaces. Each had one
    `.composer-footer`, `.composer-permission-trigger`, `.composer-token-meters`,
    `.composer-context-meter`, and composer textarea; the permission popover
    opened successfully.
  - Screenshots are stored under
    `release-artifacts/manual-test-0016-browser-smoke/`.
  - DOM inspection confirmed `.composer-zone` has no `border-top`; the only
    line near the input is the composer form's own 1px field border.
- Manual test state:
  - Desktop `release-local-0016` is running.
  - WebUI `http://127.0.0.1:9939/app/` is running and was opened in the default
    browser.
  - Upload/deploy/GitHub sync remains paused until user manual testing passes.
  - Windows signed setup remains blocked on the signing provider/private key.

## 2026-06-16 Feishu Raw CLI Bypass Coverage Follow-Up

- Follow-up review found another raw-shell bypass: the previous autoroute path
  covered `lark-cli ...` and `npx lark-cli ...`, but a model could still call
  Feishu through `npx @larksuite/cli...` or local `node .../cli-main/scripts/run.js`.
- `AgentStreamExecutor` now recognizes these simple invocations as Feishu CLI
  runners, autoroutes them into `feishu_cli action=run`, and groups them under
  the `feishu_cli:bash` chain budget. Complex forms with pipes, redirects, or
  command separators still hard-stop with Feishu CLI guidance.
- Regression coverage added:
  - `test_raw_lark_cli_bash_autoroute_covers_npx_package_and_node_runner`
  - Existing complex-command test now checks `npx @larksuite/cli ... && ...`
    is not autorouted but is still blocked with Feishu CLI guidance.
- Validation:
  - `python -m py_compile agent\protocol\agent_stream.py tests\test_ecorex_web_parallel_backend.py` passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`59` tests).
- Packaging state: all `release-local-0015` and matching WebUI/public artifacts
  are now stale after this Agent Core source change. Rebuild before the next
  hand test or upload/deploy.

## 2026-06-16 Same-Request SSE Broadcast Follow-Up

- Parallel review found the next Web/Desktop coexistence bug: the backend used
  one `Queue` per `request_id` as the actual SSE event source. A new
  EventSource also superseded the previous stream token, so two surfaces
  watching the same live request could steal or interrupt each other's events.
- WebChannel now keeps a per-request replay log (`sse_events`), condition
  variable, and subscriber count. `_push_sse_event()` appends each event once
  and wakes every subscriber; `stream_response()` reads with a per-connection
  cursor and emits SSE `id:` values for browser reconnect support.
- `sse_queues` remains as a legacy/test mirror and for stream availability
  checks, but it is no longer the consumer source for EventSource output.
- Regression coverage changed from "new connection supersedes old consumer" to
  `test_multiple_sse_connections_receive_same_request_events`, which proves two
  EventSource generators for the same `request_id` both receive the same
  terminal `done` event.
- Validation:
  - `python -m py_compile channel\web\web_channel.py tests\test_ecorex_web_parallel_backend.py` passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`58` tests).
- Packaging state: all `release-local-0014` and matching WebUI/public artifacts
  are now stale after this runtime source change. Rebuild before the next hand
  test or upload/deploy.
- Final rebuild after this follow-up:
  - `npm run typecheck`, `npm run build`, and `npm run stage:runtime:win`
    passed. The renderer hash stayed `index-CjBkNLMl.js`.
  - Desktop hand-test artifact:
    `desktop/release-local-0015/win-unpacked/EcoreX.exe`.
  - WebUI dual Win/Mac package SHA256:
    `3A5F83F8772B3524F84CFB5C179522B0E06AD15647F30F1040A0617904EBF313`.
  - WebUI Windows compatibility package SHA256:
    `FCA73F5C782683FCBDC69ED1AB4C527B2746DB0DBE29A12502C6D8F31FECFB09`.
  - WebUI macOS universal tarball SHA256:
    `2A10E3E36D206FF4BF55268294D17DB1341DF3D8FB0A8CD2F2826A2F18827573`.
  - Linux/Web service tarball SHA256:
    `20EF1A0C8E6B250FD4EFD2B3F44FABC8C4611E1DAE2A02346FA01A0FEA4F55B7`.
  - Public release zip SHA256:
    `045240D7BA63EF543F9D97920149F308F8DDC8B3C07F952A7BD7FEF9227F1ACE`.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0015\win-unpacked`
    passed.
  - Runtime smoke passed with desktop `127.0.0.1:9936` and installed WebUI
    `127.0.0.1:9937` running simultaneously. Both returned version `0.1.12`,
    served `index-CjBkNLMl.js`, exposed `feishu_cli`, `host_diagnostics`, and
    `browser`, returned `/api/active-requests`, and passed permission mode
    round-trip `read-only -> full-access`.
  - Packaged desktop and WebUI Python runtimes both passed same-request SSE
    broadcast smoke and raw `bash lark-cli` -> `feishu_cli` autoroute smoke.

## 2026-06-16 Raw Feishu CLI Autoroute Follow-Up

- Follow-up Agent Core audit found one remaining Codex-boundary gap: EcoreX
  blocked `bash lark-cli ...` and told the model to use `feishu_cli`, but a
  stubborn model could still repeat the wrong bash call. That was safer than
  executing raw shell, but it was not enough host-level help to self-correct.
- `AgentStreamExecutor` now automatically maps simple `lark-cli ...` and
  `npx lark-cli ...` shell commands into `feishu_cli` before execution. The
  rerouted call inherits the Feishu tool's permission broker, timeout,
  cancellation, packaged CLI resolution, and secret masking. The result carries
  `reroutedFrom` so logs/UI can explain why a bash request executed as
  `feishu_cli`.
- Complex shell forms containing pipes, redirects, command separators, or
  multiple commands are still hard-stopped. They are not reinterpreted as Feishu
  arguments because that would turn arbitrary shell syntax into a trusted host
  tool invocation.
- Regression coverage added:
  - `test_simple_raw_lark_cli_bash_autoroutes_to_feishu_tool`
  - `test_complex_raw_lark_cli_bash_keeps_guidance_not_autoroute`
- Validation:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`58` tests).
  - `python -m py_compile agent\protocol\agent_stream.py tests\test_ecorex_web_parallel_backend.py` passed.
- Packaging state: the previously built `release-local-0013` desktop/WebUI
  artifacts are stale after this Python runtime source change. Before the next
  manual hand-test or upload/deploy, run runtime staging, rebuild desktop
  package, regenerate WebUI/Linux/public artifacts, update manifest hashes, and
  rerun `scripts\validate-ecorex-release-artifacts.py`.
- Final rebuild after this follow-up:
  - `npm run typecheck`, `npm run build`, and `npm run stage:runtime:win`
    passed. The renderer hash stayed `index-CjBkNLMl.js`.
  - Desktop hand-test artifact:
    `desktop/release-local-0014/win-unpacked/EcoreX.exe`.
  - WebUI dual Win/Mac package SHA256:
    `7B87CFEB92C3AD3BFC24D47844CC9C739DCEC7E1A34D8D98CD16AE0BFB5C3649`.
  - WebUI Windows compatibility package SHA256:
    `4CFDCA947E36B1CFBB7F328D1ED4F4FAFAACA9AAD0971FF161C5510EF2C50B03`.
  - WebUI macOS universal tarball SHA256:
    `9C1B1C19924BAC4F534BFB0DBCACD040AA23E81372AD75A94EB2C71647A60E47`.
  - Linux/Web service tarball SHA256:
    `34927AD8FCD69DFADD7F305E692F43F9DBB0ECC3E3A7110B165D422F1A397F8A`.
  - Public release zip SHA256:
    `EAB5AB409980BB4960A1BECA0B3D97BD1A4B011306907E1DD9DA3FCCDEE26D94`.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0014\win-unpacked`
    passed.
  - Runtime smoke passed with desktop `127.0.0.1:9934` and installed WebUI
    `127.0.0.1:9935` running simultaneously. Both returned version `0.1.12`,
    served `index-CjBkNLMl.js`, exposed `feishu_cli`, `host_diagnostics`, and
    `browser`, returned `/api/active-requests`, and passed permission mode
    round-trip `read-only -> full-access`.
  - Packaged desktop and WebUI Python runtimes both autorouted a fake raw
    `bash` `lark-cli docx +read --as user` request to `feishu_cli action=run`
    with `reroutedFrom=bash:raw bash lark-cli`.
- Parallel review after rebuild agreed the autoroute fix closes the immediate
  "model repeats raw bash instead of using Feishu wrapper" gap. Remaining
  non-claim items are now explicit: same `request_id` watched by desktop and
  WebUI still needs SSE broadcast/replay instead of single-consumer stream
  replacement, and Feishu/Xiaohongshu can be hardened further with auth/page
  flow guards that stop after `authRequired` or selector `should_continue=false`.

## 2026-06-16 Active Request Runtime Snapshot

- User asked to audit whether EcoreX Agent Core and the host capability boundary
  can align with Codex. The public Codex app-server reference exposes
  thread/read, thread/list, thread/status/changed, turn/start, turn/steer,
  turn/interrupt, command/process sessions, fs APIs, skills/plugins/config, and
  MCP status/resource/tool APIs. EcoreX is now closer at the policy and
  recovery layer, but it is not yet a full Codex-equivalent host.
- Closed the largest low-risk runtime-status gap: WebChannel now exposes
  `/api/active-requests`, backed by the in-process cancel registry, so WebUI and
  desktop can ask the backend which requests are actually in flight instead of
  trusting stale localStorage-only pending state.
- `CancelTokenRegistry.snapshot()` now returns non-content request metadata:
  `request_id`, `session_id`, `cancelled`, `state`, `created_at`, and
  `age_seconds`. WebChannel enriches that with `stream_available` and a session
  -> request map.
- The shared renderer `loadRuntimeSnapshot()` now fetches active requests along
  with version, sessions, tools, skills, and model state. Session rows are marked
  running when the backend reports a live request, even if the cached frontend
  bubble was lost during refresh or cross-surface use.
- Session restore now accepts a backend `request_id`. If the cached assistant
  bubble exists, it reconnects to the same `/stream?request_id=...`; if the
  bubble was lost, it creates a minimal "connecting to backend task" assistant
  placeholder and attaches the stream to the still-running backend request.
- Parallel explorer review found the first pass was not quite closed:
  reconnect retry exhaustion could still mark a backend-active task paused,
  `cancelled=true` was hidden instead of shown as stopping, `stream_available`
  was unused, active requests outside the latest session page could disappear
  from the sidebar, and history recovery treated file/media/tool-only final
  replies as unfinished.
- Follow-up frontend fixes:
  - Session rows now preserve backend `cancelling` state and display active
    requests even when they are absent from the latest 40 session records.
  - `stream_available=false` no longer starts a normal SSE attach. The renderer
    refreshes history and waits for backend completion instead of creating an
    immediately invalid EventSource.
  - After repeated SSE reconnect failures, the renderer re-checks
    `/api/active-requests`. If the backend still reports the request active, it
    keeps the bubble pending and retries/waits instead of marking it paused.
  - If the backend reports a cancelled-but-still-registered request, the UI
    shows "backend task stopping" and waits for the token to unregister.
  - History recovery now accepts final assistant messages that have media/tool
    steps or sequence metadata even when text content is empty.
- Regression tests added:
  - `test_active_request_snapshot_reports_backend_runtime_state`
  - `test_cancel_registry_snapshot_marks_cancelled_request`
- Validation so far:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`57` tests).
  - `cd desktop && npm run typecheck` passed.
- Boundary truth: this matches the important part of Codex's runtime-status
  behavior for refresh/switch recovery while the same local process is alive.
  It still does not provide full Codex app-server parity: there is no durable
  cross-process event replay, thread/turn object model, `turn/steer`, managed
  PTY/process API, or filesystem/sandbox API.
- Agent Core boundary conclusion from the code audit:
  - EcoreX can align with Codex at the policy, diagnostics, CDP-first routing,
    tool-loop closure, permission, and current-process liveness layers.
  - EcoreX cannot yet claim Codex host parity because it lacks a durable
    app-server object model, OS/container sandbox profiles, process/PTY
    sessions, `turn/steer`, product-level sub-agents, patch/worktree
    transactions, and persistent event replay.
  - The current v0.1.12 wording should therefore remain "Codex-boundary
    inspired hardening" rather than "Codex-equivalent host".
- Final rebuild after this source change:
  - Renderer/static asset: `index-CjBkNLMl.js` with `index-BG_69rJD.css`.
  - Desktop local hand-test package:
    `desktop/release-local-0013/win-unpacked/EcoreX.exe`.
  - WebUI dual Win/Mac package SHA256:
    `05C5A508468AFAA6DF322598A8301A5748830F16BAF5E3439927C8BE040F0086`.
  - WebUI Windows compatibility package SHA256:
    `69F5B862649C5410A050E6749E07C787778C131722F6B3D0595960F3AF6753F0`.
  - WebUI macOS universal tarball SHA256:
    `F18A2C4012B55E93DBD42B0749274632CFECC5FC700E4D1334B1D7A92E46B685`.
  - Linux/Web service tarball SHA256:
    `105CDBC23DC397E1DBC14E7B6473027F13B5498D9AA5484B9B621D5547153301`.
  - Public release zip SHA256:
    `AB37AFE0845D7BF52F249E008EA3EFA89D3E7F3B41136662F2031FE27BE5A54A`.
- Final validation after rebuild:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`57` tests).
  - `cd desktop && npm run typecheck` passed.
  - `cd desktop && npm run build` passed.
  - `cd desktop && npm run stage:runtime:win` passed.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0013 --config.win.signAndEditExecutable=false` passed.
  - WebUI/Linux/public release packaging passed.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0013\win-unpacked` passed.
- Runtime smoke:
  - Desktop `release-local-0013` runs on `127.0.0.1:9932`.
  - Installed WebUI from the new dual package runs on `127.0.0.1:9933`.
  - Both return `/api/version == 0.1.12`, serve `index-CjBkNLMl.js`, expose
    `feishu_cli`, `host_diagnostics`, and `browser`, and return
    `/api/active-requests` successfully.
  - Permission mode readback passed on both runtimes with isolated audit paths.
  - In-app Browser opened `http://127.0.0.1:9933/app/` and confirmed the new
    EcoreX login page rendered; screenshot capture timed out in CDP, but DOM
    state was readable.
- Upload/deploy remains paused. Windows signed setup remains
  `pending-signature` until SimplySign/Smart Card private-key provider recovery.

## 2026-06-16 Config, Skill Diagnostics, And Post-Done Tail Closure

- User-facing root cause note: Codex self-corrects more reliably because failed
  skills, host tools, permissions, and process state are visible to the model as
  structured host state. EcoreX was still hiding some failures in logs or cached
  tool fields, so the model could keep trying `bash` without seeing the real
  blocker. This pass closes the largest remaining gaps without claiming full
  Codex parity.
- Parallel review found and fixes were applied for:
  - `FeishuCli` cached `package`, `auto_install`, and `cwd` after construction.
    ToolManager and AgentInitializer now call `apply_config()` after per-tool
    and workspace config merges.
  - `feishu_cli action=ensure` ignored `auto_install=false`; it now respects the
    computed `install_if_missing` value.
  - `HostDiagnostics` also refreshes cached `cwd` via `apply_config()`.
  - Skill loading diagnostics were only debug logs. They are now available in
    the skills prompt under `<skill_load_diagnostics>` and in
    `host_diagnostics.skills`.
  - The renderer no longer drops `voice_attach` events that arrive after SSE
    `done`, restores persisted `extras.audio` from history, refreshes cached
    sessions from runtime history, closes streams on `cancelled`, and treats
    `/uploads/...` as runtime HTTP media rather than local paths.
  - The repository static WebUI was resynced from `desktop/dist` so source
    launches and packages use the same `index-DntImxX6.js` bundle.
  - Linux Web packaging now copies `desktop/dist` contents into
    `runtime/channel/web/static/app` instead of nesting them under `app/dist`.
  - The release validator checks actual packaged `index.html` references,
    renderer sentinels, Feishu/host diagnostics config refresh, skill
    diagnostics, public manifest parity, and absence of extra stale downloads.
- Regression coverage added; `python tests\test_ecorex_web_parallel_backend.py`
  now passes `55` tests.
- Validation passed:
  - `python -m py_compile` for changed runtime and release-validator files.
  - `npm run typecheck`
  - `npm run build`
  - `npm run stage:runtime:win`
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0012 --config.win.signAndEditExecutable=false`
  - `powershell scripts\prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`
  - `powershell scripts\prepare-ecorex-web-release.ps1 -Version 0.1.12`
  - `powershell scripts\prepare-ecorex-public-release.ps1 -Version 0.1.12`
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0012\win-unpacked`
- Superseded hand-test/package set from this intermediate pass; do not use as
  current:
  - Desktop local hand-test package:
    `desktop/release-local-0012/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152995768`
    - SHA256: `47FDCBB81803053CEC413E8D2975CE8F9CD2BB828F042F7B9DE9EA6B5872ED52`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72861040`
    - SHA256: `7B86C77A0995E88E4D3EED53B058B88980FF9BD6DC01350851B4A5C5DE766186`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79785669`
    - SHA256: `09254A8ABA2B26E04DE0CBD6A486940D1BA345573BD0A5C34497BA587ACC0E6F`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3105467`
    - SHA256: `83631EDE2DA603C1AAE23EEEEC0658425B2BD243D8D323F71DACD1A63828B256`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `309891953`
    - SHA256: `4E47E9601C8B135AAF69AF29B67B4E6BE0052B41E97A36B0B067941E4A23FB93`
- Runtime smoke:
  - Desktop `release-local-0012` launched visibly and served
    `127.0.0.1:9899/app/`.
  - The rebuilt dual Win/Mac WebUI zip was extracted and installed; installed
    WebUI served `127.0.0.1:9909/app/`.
  - Both returned `/api/version == 0.1.12`, served `index-DntImxX6.js` with no
    old JS hashes, exposed `feishu_cli`, `host_diagnostics`, and `browser`, and
    permission readback passed `read-only -> full-access` with isolated audit
    paths.
- Upload/deploy remained paused until user manual testing confirmed the
  then-current hand-test pair. Windows signed setup remained pending SimplySign/Smart Card
  private-key provider recovery.

## Active Goal

- Remove session-list filler summaries such as "????".
- Keep active tasks running across session switches, page refreshes, and short
  SSE disconnects; reconnect by persisted `request_id`.
- Convert stale cached requests from a fully stopped runtime into a paused
  assistant bubble instead of leaving `???`.
- Fix dead shared session locks whose owner PID no longer exists.
- Investigate the then-latest Xiaohongshu note skill loop and decide whether the
  root cause is performance, tool-call chaining, permission/lock/SSE state, or
  skill prompt design.
- Investigate recent half-output stalls where the assistant stops mid-answer.
- Compare current EcoreX WebUI/runtime behavior with the source/legacy Web
  implementation for SSE, tool calls, capability exposure, media rendering, and
  performance.
- After fixes, run parallel agent review until findings agree, then build formal
  WebUI and desktop packages for manual testing. Only deploy/upload release
  assets after manual confirmation.

## Current Sub-Agent Findings

- Agent A found that backend SSE detach no longer cancels tasks, but frontend
  refresh/re-entry can lose the live session because cached live sessions are
  not merged into the sidebar. It also found that initial stream `onError` did
  not clear stale cleanup or schedule reconnect.
- Agent B confirmed the permission menu, token/context footer, divider removal,
  build hash alignment, and manifest hashes. It found dead lock files under the
  shared EcoreX workspace and warned that direct `deploy/ecorex-site` sync lacks
  local download files unless the public release zip is used.
- Agents C and D are currently analyzing Xiaohongshu skill loop and half-output
  stall roots.
- Agent E is currently comparing source/legacy Web capability and SSE behavior.

## Fixes In Progress

- Frontend boot restores the last active or live cached session from
  `localStorage`, so a page refresh can still discover a live `requestId`.
- `mapSessions()` merges local cached sessions, including live pending sessions
  not yet present in runtime history.
- Session row details are blank unless there is meaningful project context; do
  not show "????" or other generic filler.
- Stream attachment now retries the same `request_id` after non-terminal
  EventSource errors. Stale cleanup handles are removed before retry.
- `SessionLock` removes locks whose recorded PID is not alive on the same host,
  without waiting for the six-hour stale timeout.
- WebChannel pre-registers the Web `request_id` in the cancel registry before
  the worker thread enters AgentBridge. A same-session follow-up can therefore
  interrupt even if it arrives during agent startup/tool loading.
- SSE now tracks the latest attached stream token per `request_id`; an old
  EventSource cannot continue consuming the single backend queue after a page
  refresh or session re-entry attaches a newer stream.
- Config saves through `/config` now re-apply EcoreX runtime defaults before
  writing `config.json`, so model/API-key changes cannot erase the CDP-first
  `tools.browser` and `chrome-devtools` MCP defaults from disk.
- React message rendering no longer folds repeated tool rows by tool name.
  Repeated `bash`/`browser` calls stay visible as distinct executions, so a
  later running or failed call cannot visually overwrite earlier completed
  tool output.
- Chat rendering now supports audio media steps and bare image/video/audio URLs
  in Markdown content. WebUI and desktop share the same renderer, so generated
  images, local file previews, videos, and voice attachments do not diverge.
- The Xiaohongshu skill runtime copies under both `C:\Users\user\EcoreX\skills`
  and `C:\Users\user\.codex\skills` now contain explicit Feishu Base
  convergence rules. `has_more=true` is treated as a pagination hint, not a
  command to keep reading forever.
- Added `scripts/select_feishu_references.py` to the Xiaohongshu skill. It
  maps the real `lark-cli` `fields` + row-array JSON shape into compact
  records, scores relevance against the current brief, emits
  `should_continue_pagination`, and enforces a three-page hard cap.
- Added the Xiaohongshu skill to the repository `skills/create-xiaohongshu-note`
  packaging source (without generated `__pycache__`) so future desktop/WebUI
  runtime staging carries the same convergence rules.
- Default `agent_workspace` is now `~/EcoreX` in source defaults,
  `config-template.json`, cloud-client fallbacks, and `_ensure_ecorex_runtime_defaults`.
  The staged runtime config object now contains the value directly instead of
  relying only on log-time fallback. Legacy `~/cow` remains only as a migration
  source for old data.

## Confirmed Root Causes

- The then-latest WebUI log showed browser automation failing before CDP could run:
  `Playwright Python package is not installed`. CDP remains the first browser
  automation path; `playwright` is the required CDP client package, while
  `playwright install chromium` remains only the managed-Chromium fallback pack.
- The system-disk task was not just a frontend spinner. The log shows recursive
  PowerShell size scans timing out after long wall time, and those failures were
  followed by additional turns. `agent/tools/bash/bash.py` now kills the process
  tree on timeout, and the remaining work is to verify this in the final staged
  runtime.
- The "session busy" / no insertion behavior can happen when the second message
  arrives while the first request has acquired the session lock but has not yet
  reached AgentBridge registration. Pre-registering the Web request closes that
  window.
- Half-output / paused UI can happen when multiple EventSource connections for
  one `request_id` race on the same backend queue. The stream token fix from
  that pass
  prevents detached old connections from consuming future terminal events.
- A repeated-tool display bug could make status appear wrong: the React message
  component previously compacted all tools with the same name into one row.
  Keeping every tool row aligns the WebUI with the legacy Web stream renderer
  and makes long tool chains auditable.
- The then-latest Xiaohongshu loop was a successful-tool convergence bug, not a
  CDP/browser-control bug. The Feishu Base command returned valid pages with
  `has_more=true`; EcoreX's loop guards caught repeated failures and identical
  calls, but did not stop successful pagination when the offset/filter changed.
  Codex converged more often because its host skill loader, tool budget,
  truncation, and self-check behavior are stricter. EcoreX must therefore
  enforce convergence in the skill/runtime, not rely only on model discretion.
- Codex and EcoreX can read different skill copies. Codex normally uses
  `C:\Users\user\.codex\skills\...`; EcoreX can use
  `C:\Users\user\EcoreX\skills\...` or packaged skill copies. Any skill fix
  that affects product behavior must be synced into the EcoreX runtime copy and
  verified in the staged package.

## Validation Added

- `npm run typecheck` passed after the message renderer changes.
- `python -m py_compile` passed for `channel/web/web_channel.py`,
  `common/ecorex_workspace.py`, `tests/test_ecorex_web_parallel_backend.py`,
  and both Xiaohongshu `select_feishu_references.py` copies.
- Staged runtime validation confirms `agent_workspace=~/EcoreX`,
  `tools.browser.cdp_endpoint=http://127.0.0.1:9222`, `chrome-devtools` MCP,
  `playwright`, and `croniter` are present.
- The shared React renderer was rebuilt after the compact header/session-list
  UI adjustment. The bundle for that validation pass was:
  - `index-C30Hbyh1.js`
  - `index-BG_69rJD.css`
- `channel/web/static/app` is synced from the same `desktop/dist` build, so the
  WebUI package and desktop sidecar serve the same UI code.
- The UI density pass reduced the chat header from 58px to 48px,
  reduces chat status/account pills to 28px height, and reduces session-list
  title text to 13px to leave more vertical space for chat content.
- Formal local smoke test after rebuilding packages:
  - Desktop release runtime listens on `127.0.0.1:9899`.
  - Installed WebUI runtime listens on `127.0.0.1:9909`.
  - Both `/api/version` endpoints return `0.1.12`.
  - Both ports serve `/app/` and the same JS/CSS asset hashes.
  - Both ports report `full-access` from `/api/tool-permissions`.
  - Both ports expose the browser tool with CDP/Chrome DevTools as the first
    control path.
- Real stuck-task sample regression:
  - Weak brief `????? ??` returns selected records but
    `should_continue_pagination=true`, because keyword coverage is weak.
  - Relevant brief `???? ???? ??` returns
    `should_continue_pagination=false`.
  - Weak brief with `--page-count 3 --max-pages 3` returns
    `should_continue_pagination=false`, closing the loop with a gap report
    instead of paging forever.

## Formal Package State

- `release-artifacts/EcoreX_0.1.12_x64-setup.exe`
  - size: `149,075,992`
  - SHA256: `731DD1BEA86FAEE56F286D6AAB649723DA6103C82A05D8D7B750BD557832C2A3`
  - Authenticode status: stale signed artifact from before the stricter
    config/log masking rebuild. Do not publish as the v0.1.12 current artifact.
  - The default-output `desktop/release/win-unpacked` was rebuilt but unsigned because
    `signtool /debug` reaches `After Private Key filter, 0 certs were left`.
- `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
  - size: `152,971,691`
  - SHA256: `77C46E33799A0A87E875D12E56EB68229F82682591405C4C155817E28B175FB5`
  - Contains Windows and macOS WebUI installers plus the same runtime UI assets.
- `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
  - size: `72,849,007`
  - SHA256: `6F8E274281C603596DB0E0CA0AFD5462084ECC9B08753202435B2C618849E9FD`
- `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
  - size: `79,775,944`
  - SHA256: `00238A7D8ADB062E169B8350ED95F07218715D9412ADCE6DC07670D531CAF944`
- `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
  - size: `3,397,610`
  - SHA256: `922861D71D1EDE9B40DD8CFCF83FA63764F3A5ABF408E33DE77DE627EFBDED1A`
- `release-artifacts/EcoreX_0.1.12-public-release.zip`
  - size: `310,135,214`
  - SHA256: `E2BF0F16F14437C85E0B6110674D0992FCB36FD0270D02979D59E2479B16827F`
  - Pending-Windows validation bundle only; it excludes Windows desktop while
    `windows-x64` is `pending-signature`.
- The desktop blockmap from the packaging tool was not copied to
  `release-artifacts`; signing changes the final EXE hash, so do not advertise
  an auto-update blockmap unless it is regenerated after signing.

## Remaining Work Before Upload

- Wait for manual testing of the freshly rebuilt formal packages.
- After manual confirmation, update public download/admin release metadata if
  needed, then commit, push, and deploy/upload. Do not upload before the manual
  package test passes.

## Release Gate

- Do not make another final GitHub/release upload until the user manually tests
  a freshly rebuilt formal package.
- Required local checks before manual test:
  - `npm run typecheck`
  - `npm run build`
  - `python -m py_compile` for changed runtime files
  - WebUI static sync contains the latest JS/CSS hash
  - WebUI and desktop can run together on their separate ports
  - all permission modes can be set/read on both ports
  - no dead PID lock prevents a new request
  - public release zip contains all v0.1.12 download files if it is used for
    deployment

## 2026-06-15 Host Boundary Update

- Added `docs/ecorex/v0.1.12/agent-host-boundary-audit.md` to record the
  EcoreX vs Codex host capability boundary and the root reason EcoreX could
  keep probing through `bash` while Codex can often self-correct through
  structured host APIs.
- Added read-only `host_diagnostics` as a model-visible tool so the agent can
  inspect sanitized runtime, CDP, MCP, permission, Feishu, and recent log state
  before falling back to raw shell commands.
- Added `feishu_cli` as the first-class Feishu/Lark tool path and made it read
  `tools.feishu_cli.package` plus `tools.feishu_cli.auto_install`.
- Added tool-chain convergence protection in `AgentStreamExecutor` for repeated
  Feishu, browser/CDP, and shell chains whose arguments keep changing but do
  not converge.
- Changed chrome-devtools MCP defaults from `--autoConnect` to explicit
  `--browserUrl http://127.0.0.1:9222 --no-usage-statistics`, keeping BrowserTool
  and MCP on the same CDP-first boundary.
- MCP stdio/SSE/streamable-http startup now goes through noninteractive
  permission checks. MCP tool execution is mapped to `browser` for
  `chrome-devtools` and to `mcp` for other servers.
- Dangerous tool permission-check failures now fail closed instead of silently
  allowing execution; safe tools still continue if the broker cannot be loaded.
- Config/log masking now recursively masks nested `token`, `password`, and
  `authorization` fields in addition to key/secret values.
- Windows/macOS WebUI packaging scripts now preinstall or fallback-install
  Feishu CLI. Windows can bundle `C:\cli-main\bin\lark-cli.exe`; macOS needs
  `ECOREX_LARK_CLI_DARWIN` for a fully offline bundled binary, otherwise the
  installer logs the npm fallback.
- Rebuilt all formal artifacts after the host-boundary update:
  - Windows runtime now includes `host_diagnostics`, `feishu_cli`, and bundled
    `tools/bin/lark-cli.exe`.
  - WebUI Windows/macOS packages carry the same runtime/tool defaults and CDP
    `--browserUrl` configuration.
  - `config-template.json` uses an ASCII `character_desc` to avoid Windows
    PowerShell 5 default ANSI decoding corrupting UTF-8 Chinese JSON during
    package validation or installer-side config inspection.
  - Linux service package and public-release zip were regenerated from the new
    manifest.
  - Do not upload yet; these package hashes are for the next manual test gate.

## 2026-06-15 Host Boundary Package Verification

- `npm run typecheck` passed.
- `npm run build` passed and produced shared renderer assets:
  - `index-C30Hbyh1.js`
  - `index-BG_69rJD.css`
- `python -m py_compile` passed for changed agent/runtime files:
  `agent_stream.py`, `mcp_client.py`, `mcp_tool.py`, `feishu_cli.py`,
  `host_diagnostics.py`, `ecorex_tool_permissions.py`, `config.py`, and
  `web_channel.py`.
- Unit regression `python tests\test_ecorex_web_parallel_backend.py` passed.
- PowerShell parse checks passed for the WebUI local release and Windows runtime
  staging scripts; `bash -n` passed for macOS shell installers.
- `npx.cmd chrome-devtools-mcp@latest --help` confirms `--browserUrl` is a
  supported chrome-devtools MCP option.
- WebUI dual package structure check passed:
  - Windows and macOS runtime copies include `host_diagnostics` and
    `feishu_cli`.
  - Windows WebUI package includes `tools/bin/lark-cli.exe`.
  - macOS installer files keep executable mode.
  - Runtime config uses `--browserUrl` and no longer uses `--autoConnect`.
- Linux service tarball check passed with `CHECK_INSTALLED=0 CHECK_HTTP=0`.
- Public release zip validation passed and contains all ready v0.1.12 artifacts.
- Fixed `scripts/check-ecorex-web-release.sh` package listing detection to use
  a here-string with `grep -Fq`. The previous `printf ... | grep -q` check
  could fail under `set -o pipefail` when `grep -q` exited early and `printf`
  received SIGPIPE, producing a false missing-file result for Caddy route files.
- Earlier manual-test launch state after the rebuild:
  - Desktop `win-unpacked` runtime listened on `127.0.0.1:9899`.
  - Installed WebUI runtime listened on `127.0.0.1:9909`.
  - Both `/api/version` endpoints returned `0.1.12`.
  - Important: a later 2026-06-15 recheck showed the currently running desktop
    and WebUI processes can still serve the old hashed JS if they were not
    restarted or reinstalled after the latest static sync. Do not treat a
    running old process as proof that the rebuilt package is stale; verify the
    package contents or start a clean runtime from the rebuilt artifact.
  - The intended final state remains that desktop and WebUI `/app/`, `/chat`,
    and `/` return the same React UI asset hashes and do not serve the legacy
    `chat.html` page after the fresh package is launched.
  - Both `/api/tools` lists contain `host_diagnostics`, `feishu_cli`, and
    `browser`.
  - Permission mode isolation was rechecked after reinstall: setting desktop
    to `read-only` did not change WebUI; setting WebUI to `read-only` did not
    change desktop; both were restored to `full-access`.
  - WebUI log shows chrome-devtools MCP ready with 29 tools.
- `deploy/ecorex-site/manifest.json` stores the Chinese Web artifact labels as
  JSON Unicode escapes. Browser/UTF-8 readers still display `????? and
  `???????????????`, while Windows PowerShell 5 default `Get-Content |
  ConvertFrom-Json` remains valid.
- Config/log masking now fully replaces sensitive string values with `***`
  instead of preserving the first/last characters. This removed API-key suffix
  fingerprints from future startup logs.
- Windows signing is the current external blocker for a final formal installer:
  the certificate appears in `Cert:\CurrentUser\My` with `HasPrivateKey=True`,
  but `signtool` cannot access the private key provider. Retry signing after
  the Certum/SimplySign session is restored, then rebuild the NSIS setup and
  public release zip before upload.
- Additional signing recovery attempt:
  - `Start-Service SCardSvr` failed in the current shell with
    `Cannot open SCardSvr service on computer '.'`, so Smart Card service
    startup likely needs an elevated/admin session.
  - `SimplySignDesktop.exe` and `proCertumSmartSign.exe` were both running.
  - `certutil -user -key -csp "SimplySign CSP"` completed but listed no key
    container.
  - `signtool /debug /sha1 0F678477DFC0A2BDAAB88307126EF657FAF8674F`
    still ended with `After Private Key filter, 0 certs were left`.
  - Next recovery path: unlock/login in the Certum/SimplySign UI and/or start
    Smart Card services from an elevated shell, then rerun
    `npm run sign:win:unpacked`, prepackaged NSIS, and `npm run sign:win:setup`.
- Added a signing preflight to `desktop/scripts/sign-win.ps1` and wired
  `desktop/package.json` so `npm run package:win:signed` starts with
  `npm run sign:win:preflight`. In the current environment it fails fast with
  Smart Card service `Stopped` and no visible SimplySign CSP key containers,
  instead of spending minutes building before `signtool` reaches the same
  private-key failure.

## 2026-06-15 Agent Core Follow-Up

- Parallel read-only audits agreed that the remaining Xiaohongshu/Feishu stall
  class was not only a skill bug. It was a host-boundary bug: the model could
  still slide back to raw `bash` probing even though EcoreX now had better
  structured host tools.
- Strengthened the main system prompt so `host_diagnostics`, `feishu_cli`, and
  CDP-first browser control are explicit host-boundary rules, not only optional
  tool names. The prompt now tells the model to stop repeating the same
  external capability chain, name the blocker, switch approach, or ask for
  login/authorization/input.
- Added hard pre-execution routing in `AgentStreamExecutor`: simple raw
  `bash lark-cli ...` calls now execute through `feishu_cli`; complex raw
  Feishu shell and raw CDP probing via shell are blocked and point the model to
  `feishu_cli`, `host_diagnostics`, and the configured browser/CDP path.
- Tool short-circuit paths now emit `tool_execution_start` and
  `tool_execution_end` too. This covers JSON-parse errors, loop-budget stops,
  tool-not-found, permission denial, and host-boundary reroutes so the UI can
  render a concrete tool row instead of leaving a vague thinking state.
- Default chrome-devtools MCP startup is allowed only through
  `authorize_noninteractive` with reason `default-cdp-mcp-startup`; actual MCP
  browser tool calls still map to the normal `browser` permission category.
- `self_evolution_enabled` default is now aligned with the template (`true`),
  and `host_diagnostics` reports the current self-evolution switch. This helps
  distinguish current-run convergence from idle post-run skill/memory repair.
- Desktop/WebUI stream handling is now request-aware. EventSource cleanup is
  bound to the request id, stale stream terminal events cannot clear a newer
  request, reconnect timers exit when their request is no longer current, and
  first-send plus reconnect paths both treat stale `invalid request_id` as a
  paused message.
- Running tool rows no longer become `done` when a request is paused,
  cancelled, or errors. They now finish as `paused`, `cancelled`, or `error`
  according to the real terminal state.
- Added regression coverage:
  - prompt contains host-boundary rules;
  - Feishu chain-budget stops repeated probing;
  - raw `lark-cli` bash is grouped and rerouted;
  - default chrome-devtools MCP startup passes noninteractive authorization.
- Checks run after this follow-up:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`13` tests).
  - `python -m py_compile agent\protocol\agent_stream.py agent\prompt\builder.py common\ecorex_tool_permissions.py agent\tools\host_diagnostics\host_diagnostics.py` passed.
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build` in `desktop/` passed and the WebUI static app was synced.
    Initial asset was `index-C30Hbyh1.js`; after fixing a real tooltip mojibake
    in `MessageContent.tsx`, the asset for that pass was `index-Crsnr3ve.js` plus
    `index-BG_69rJD.css`.

## 2026-06-15 Package Recheck After Agent-Core Follow-Up

- Rebuilt WebUI packages with the then-synced static assets:
  - `EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152974855`
    - SHA256: `A5B221E7020CB1E51D3C2F06C6C18C8BAD13A3EE5ECB44B9F5FEB554C9750FA0`
  - `EcoreX_0.1.12-public-release.zip`
    - Size: `310139078`
    - SHA256: `72058421ACCE46523FBD0C79A8927795604D8E7DCA3B276AAE66E28CA4AB33BF`
- Package structure checks confirmed the dual Win/Mac WebUI zip contains:
  `index-Crsnr3ve.js`, `index-BG_69rJD.css`, `host_diagnostics`,
  `feishu_cli`, and Windows `tools/bin/lark-cli.exe`.
- Clean temporary WebUI launch from the rebuilt zip used port `9915` and
  returned:
  - `/app/` status `200`
  - New asset present: `index-Crsnr3ve.js`
  - Old assets absent: `index-C30Hbyh1.js`, `index-C5m21jCF.js`,
    `index-CdKabNii.js`, `index-dSHNqlZq.js`
  - `/api/version`: `0.1.12`
  - `/api/tools`: contains `feishu_cli` and `host_diagnostics`
  - Startup log: chrome-devtools MCP ready with `29` tools.
- At that time, already-running local instances were still old process
  instances:
  - Installed WebUI process: `127.0.0.1:9909`, from
    `%LOCALAPPDATA%\EcoreX WebUI\runtime\python\python.exe`.
  - Desktop sidecar process: `127.0.0.1:9899`, from
    `desktop\release\win-unpacked`.
  - They can keep serving old hashed JS until restarted or replaced with the
    rebuilt package. This is the root cause of "opened WebUI but still old page"
    during hand testing.
- Same-device coexistence recheck: installed WebUI and desktop sidecar were
  both listening at the same time on different ports (`9909` and `9899`);
  no port collision was observed.
- Desktop local package recheck:
  - `desktop\release-local-2300\win-unpacked` was generated as a runnable,
    unsigned local test package.
  - It contains `app.asar` with `index-Crsnr3ve.js` and `index-BG_69rJD.css`.
  - It contains runtime `host_diagnostics`, `feishu_cli`, and
    `tools/bin/lark-cli.exe`.
  - Authenticode status is `NotSigned`; do not publish this as the formal
    Windows installer.
- Desktop local hand-test launch for that pass:
  - Started `desktop\release-local-2300\win-unpacked\EcoreX.exe` with
    `ECOREX_WEB_PORT=9916` so it could run beside the older desktop sidecar on
    `9899` and the installed WebUI on `9909`.
  - `127.0.0.1:9916/api/version` returned `0.1.12`.
  - `127.0.0.1:9916/app/` served `index-Crsnr3ve.js` and did not serve the old
    hashed assets.
  - `127.0.0.1:9916/api/tools` contained `feishu_cli`,
    `host_diagnostics`, and `browser`.
  - Permission API state was verified on that desktop sidecar:
    `full-access -> read-only -> full-access`, with each state read back from
    `/api/tool-permissions` after the write.
- Important packaging pitfall found and fixed: after rebuilding renderer/static
  assets, run `npm run stage:runtime:win` before `electron-builder`. The
  desktop app's own `app.asar` can contain the new React asset while the
  packaged sidecar `resources/ecorex-runtime/channel/web/static/app` still
  serves an older JS hash if the runtime is not re-staged.
- Windows signing remains externally blocked:
  - `npm run sign:win:preflight` fails because Smart Card service and
    Certificate Propagation service are stopped, and no SimplySign CSP key
    containers are visible.
  - Recheck after the `release-local-2300` rebuild produced the same
    preflight failure, so no signed Windows setup was generated.
  - `electron-builder --win --dir` to the default output is blocked by the
    currently running old `desktop\release\win-unpacked\EcoreX.exe`.
  - Building to `release-local` with executable resource editing hit a repeat
    Windows `resEdit` open failure on `EcoreX.exe`; a local runnable package was
    still produced by skipping executable resource editing, but this is not a
    publishable signed installer path.

## 2026-06-15 Agent Boundary Convergence Follow-Up

- Re-audited EcoreX's agent core and host capability boundary against Codex.
  The important remaining gap was not tool presence; it was that EcoreX still
  relied too much on model-level hints after a tool chain stopped converging.
- Added a host-level one-turn text-only circuit breaker in
  `AgentStreamExecutor`. After repeated identical failures, repeated successful
  same-argument calls, or exhausted Feishu/browser/shell chain budgets, the next
  LLM request withholds tool schemas so the assistant must summarize progress
  or state the blocker instead of calling another tool.
- Kept raw `bash lark-cli ...` routing separate from this breaker. Simple
  commands now continue through `feishu_cli` in the same tool turn; complex
  shell forms are stopped with guidance because the correct next action is a
  structured Feishu tool call, not another shell probe.
- Updated the host-boundary prompt rule for skill repair: when a built-in skill
  causes a structural loop, create and patch a same-name workspace skill
  override instead of editing packaged runtime files. This matches the existing
  `SkillManager` precedence where workspace/custom skills override built-ins.
- Added regression coverage for the new convergence breaker:
  chain-budget stop sets the text-only latch, and the next model request sends
  no tool schema exactly once.

## 2026-06-15 Host Boundary Safety Follow-Up

- Made `bash` subprocess execution cancel-aware. The tool now polls the child
  process, checks the request cancel event, and kills the process tree on cancel
  or timeout instead of staying blocked inside `communicate()`.
- Made `feishu_cli` subprocess execution cancel-aware for install/ensure/auth
  and `run` paths. This closes the likely root cause of Feishu/Xiaohongshu
  tasks getting stuck in a running tool row after the user has stopped or moved
  on.
- Tightened `feishu_cli` discovery for local packaged CLI roots, including the
  Windows `lark-cli.exe` and `lark-cli.cmd` variants.
- Fixed MCP error semantics: JSON-RPC `error` and MCP `isError=true` now become
  `ToolResult.error` instead of successful text starting with `Error:`.
- Fixed MCP discovery semantics: `tools/list` JSON-RPC errors now bubble out
  so the server is marked failed instead of ready with an empty tool list.
- Added cancel propagation into `McpTool` and stdio MCP waits. A cancelled MCP
  stdio call now shuts down the server process instead of waiting for the full
  MCP timeout.
- Added cancel polling around BrowserService queued operations so a long browser
  wait/click/evaluate does not keep the request UI stuck after Stop/new message.
- Extended permission boundaries so `read-only` blocks `write`, `edit`,
  `fs_write`, and `skill_write`, not only shell/browser/Feishu/MCP tools.
- Hardened `SkillService` install boundaries. Skill names, URL payload paths,
  and zip members are now validated so remote skill packages cannot write
  outside the workspace skill directory.
- `SkillService` now also rejects Windows reserved skill names, trailing-dot
  names, silent overwrite of existing custom skills, and silent built-in skill
  shadowing unless an explicit replace/override flag is supplied.
- Changed builtin skill startup sync to preserve existing workspace overlays.
  This keeps runtime skill repairs durable and prevents app launch from
  overwriting the fixed same-name skill copy.
- Updated the packaged and active EcoreX `create-xiaohongshu-note` skill to use
  bounded Feishu setup/auth and to avoid raw `bash lark-cli` fallback while
  `feishu_cli` is available.
- Validation after this follow-up:
  - `python -m py_compile agent\protocol\agent_stream.py agent\tools\bash\bash.py agent\tools\feishu_cli\feishu_cli.py agent\tools\mcp\mcp_client.py agent\tools\mcp\mcp_tool.py agent\skills\service.py app.py agent\prompt\builder.py tests\test_ecorex_web_parallel_backend.py` passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`24` tests).

No release package had been rebuilt from those source changes yet; the
previous local hand-test package is stale until the next formal build.

## 2026-06-16 Local Desktop Hand-Test Package After Boundary Fixes

- Re-ran desktop verification after the second host-boundary pass:
  - `npm run typecheck` passed.
  - `npm run build` passed; renderer asset remains `index-Crsnr3ve.js` plus
    `index-BG_69rJD.css`.
  - `npm run stage:runtime:win` passed and bundled Windows `lark-cli.exe` under
    `resources/ecorex-runtime/tools/bin/lark-cli.exe`.
  - `npx electron-builder --win --dir --publish never --config.directories.output=release-local-0004 --config.win.signAndEditExecutable=false` produced
    `desktop/release-local-0004/win-unpacked/EcoreX.exe`.
- Package content checks confirmed:
  - packaged WebUI static runtime serves `index-Crsnr3ve.js`;
  - packaged runtime contains `host_diagnostics`, `feishu_cli`, MCP cancel/error
    fixes, and SkillService boundary fixes.
- Launched `desktop/release-local-0004/win-unpacked/EcoreX.exe` with
  `ECOREX_WEB_PORT=9918` for hand testing beside old instances.
  - `http://127.0.0.1:9918/api/version` returned `0.1.12`.
  - `http://127.0.0.1:9918/app/` references `index-Crsnr3ve.js`.
  - `/api/tools` contains `feishu_cli`, `host_diagnostics`, and `browser`.
  - `/api/tool-permissions` mode switching read back correctly:
    `read-only -> read-only -> full-access`.
- This is an unsigned local runnable hand-test package. It is not a formal
  signed Windows installer and must not be uploaded as the public release.

## 2026-06-16 WebUI Package Rebuild After Boundary Fixes

- Rebuilt the local WebUI Win/Mac packages after that agent host-boundary
  fixes with:
  `powershell -ExecutionPolicy Bypass -File scripts\prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`.
- WebUI artifacts for this now-superseded rebuild:
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152982335`
    - SHA256: `0ACFAD09B3561490FB725F9CEF3C61C774BABFBBC5E856CA6A26AEC983F4CB9F`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72854324`
    - SHA256: `A54C6901AE5E6F01C71683FE9B1BB1C2CB65E0F53165BCF91F2B8AEAA1978ABC`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79781375`
    - SHA256: `B529FED5499FB2AA1891CE8CF058E214520C86FD24ED3E1BF8213FE92929E282`
- Extracted the dual package to
  `release-artifacts/manual-test-webui-boundary-0.1.12/ecorex-webui-win-mac-0.1.12`
  and launched the packaged Windows runtime directly on `127.0.0.1:9920`
  to avoid reusing any older installed WebUI process.
- Clean package runtime verification:
  - `http://127.0.0.1:9920/api/version` returned `0.1.12`.
  - `http://127.0.0.1:9920/app/` served `index-Crsnr3ve.js` and
    `index-BG_69rJD.css`.
  - Old assets were not served: `index-dSHNqlZq.js`, `index-DBjPv6j0.css`,
    and `index-C30Hbyh1.js`.
  - `/api/tools` contains `feishu_cli`, `host_diagnostics`, and `browser`.
  - `/api/tool-permissions` mode switching read back correctly:
    `read-only -> read-only -> full-access`.
  - Permission audit file is isolated at
    `C:\Users\user\AppData\Local\EcoreX\permissions\permission-audit.jsonl`.
- Same-device coexistence check with the desktop hand-test runtime:
  - Desktop package: `127.0.0.1:9918`, audit path
    `C:\Users\user\AppData\Roaming\ecorex-desktop\permission-audit.jsonl`.
  - WebUI package runtime: `127.0.0.1:9920`, audit path
    `C:\Users\user\AppData\Local\EcoreX\permissions\permission-audit.jsonl`.
  - Both returned `full-access` independently after switching, confirming no
    port collision and no shared permission-state collision.
## 2026-06-16 Linux/Public Release Rebuild After WebUI Package Rebuild

- Rebuilt the Linux Web service package from the same source tree as that
  WebUI rebuild:
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3404135`
    - SHA256: `EBBABDE60D240DBC09CF784ABB64239CD61AC50195C51D95735BA187BADC130A`
    - Web build source: `desktop-renderer-build`.
- Updated `deploy/ecorex-site/manifest.json` to the then-current WebUI and Linux
  package sizes/hashes before rebuilding the public release zip.
- Fixed `scripts/prepare-ecorex-public-release.ps1` to write `checksums.json`
  as UTF-8 without BOM. The previous PowerShell `Set-Content -Encoding UTF8`
  output created a BOM that failed strict Python `json.loads(...decode("utf-8"))`.
- Rebuilt the public release zip:
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `310168276`
    - SHA256: `E019E1814DEBA5CBB58A21EA313797F5389C8D0735F58CDB61EE10F14B71743E`
    - Includes ready artifacts: `webui-win-mac`, `webui-windows-x64`,
      `webui-macos-universal`, and `web-linux-service`.
    - Excludes pending artifacts: `windows-x64`, `macos-arm64-dmg`, and
      `macos-x64-dmg`.
- Validation:
  - Strict Python JSON parsing of `site/manifest.json` and `checksums.json`
    inside the public zip passed, and both files are BOM-free.
  - Public zip download artifacts match manifest size/SHA values.
  - Linux service tarball contains that rebuild's `index-Crsnr3ve.js`,
    `index-BG_69rJD.css`, `feishu_cli`, `host_diagnostics`, MCP fixes, and
    Xiaohongshu skill copy.
  - Temporary local install smoke using `install-ecorex-public-release.sh`
    passed with `RESTART_SERVICE=0`; `check-ecorex-server-release.sh` passed
    with `CHECK_PUBLIC=0 CHECK_CADDY=0`.
  - Added and ran `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12`.
    It validates ready/pending artifact boundaries, manifest/download size and
    SHA matches, BOM-free JSON, stale WebUI asset rejection, required WebUI
    assets, and required host-boundary runtime files in the Linux/macOS
    tarballs.

## 2026-06-16 Signing Preflight Recheck

- `npm run sign:win:preflight` still fails in the current desktop environment:
  - Smart Card service: `Stopped`
  - Certificate Propagation service: `Stopped`
  - SimplySign CSP key containers: none visible
- Do not publish any older signed `EcoreX_0.1.12_x64-setup.exe` as the current
  v0.1.12 artifact. The current hand-test desktop package is the unsigned
  `desktop/release-local-0023/win-unpacked/EcoreX.exe`; signing provider
  recovery is still required before a publishable NSIS setup exists.

## 2026-06-16 Parallel Review P1 Boundary Fixes

- Read-only sub-agent review found no new P0 but identified five P1 boundary
  risks. All five were fixed before the next package rebuild:
  - `chrome-devtools` MCP noninteractive startup no longer trusts only the
    server name. It must match the built-in command signature
    `npx/npx.cmd chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222 --no-usage-statistics`
    and include the internal trusted-default marker. `read-only` now blocks it
    before the default startup exception.
  - `SkillService` add/open/close/delete now goes through `skill_write`
    noninteractive authorization. In `smart-ask` or `always-ask` without an
    interactive UI decision, skill mutation is blocked instead of silently
    changing agent behavior.
  - Bash `timeout` is normalized before process launch. Invalid, null, negative,
    or huge values are clamped to a safe 1-600 second range.
  - BrowserService no longer starts a second worker when a cancelled/timed-out
    Playwright worker is still shutting down. The next request receives a clear
    "worker still shutting down" error rather than sharing state with an old
    worker.
  - MCP stdio now starts in a separate process group and `shutdown()` kills the
    process tree. Streamable-http SSE response reading has a total deadline, so
    keepalive comments cannot keep a tool call alive indefinitely.
- Added regression coverage. `python tests\test_ecorex_web_parallel_backend.py`
  now runs `28` tests and passed after these fixes.
- Added `scripts/validate-ecorex-release-artifacts.py` and reran it after the
  package rebuild; it passed and now verifies WebUI ZIP packages also contain
  required host-boundary runtime files, not only frontend assets.
- Rebuilt packages after the P1 fixes:
  - Desktop local hand-test package:
    `desktop/release-local-0005/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152984755`
    - SHA256: `2197A3C7B081D8DA81950820FC4809DA7B93035CF4BB487B05F359C289603203`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72855537`
    - SHA256: `BF85E98D4D87BB7203A50800A7EDF2174589554DF59526E8AD2407AD58D48442`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79783958`
    - SHA256: `6224748A507FB92602AE71993111AC69AE96F4B1C90DA8B8D4A9140425FF5E32`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3405301`
    - SHA256: `749668E43088641257A1B1A4BF9A58A56007BC145316C3266D4BF20B8E8ED12D`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `310174737`
    - SHA256: `FED698F33B5EBA078E783421DE87C38814DCC1825FB1C09131397ECD4E6678E7`
- Runtime verification:
  - Desktop `release-local-0005` launched on `127.0.0.1:9922`.
  - Freshly extracted WebUI package launched on `127.0.0.1:9923`.
  - Both returned `/api/version == 0.1.12`, served `index-Crsnr3ve.js` and
    `index-BG_69rJD.css`, did not serve known old assets, exposed
    `feishu_cli`, `host_diagnostics`, and `browser`, and kept isolated
    permission audit paths.
- The stale signed setup previously at top-level
  `release-artifacts/EcoreX_0.1.12_x64-setup.exe` was moved to
  `release-artifacts/stale-do-not-publish/EcoreX_0.1.12_x64-setup.exe`.
  Do not upload the quarantine directory or the old setup as a current Windows
  artifact.

## 2026-06-16 Background Evolution Permission Gate

- While re-checking why Codex can self-correct skill/runtime issues while
  EcoreX can appear stuck, found one remaining host-boundary gap outside the
  foreground chat path: self-evolution is disabled by default, but if enabled it
  starts an unattended review agent with `write`, `edit`, and `bash` available.
  Without a visible UI event channel, those tools could wait on an interactive
  permission prompt instead of failing fast.
- Added `_authorize_background_evolution()` in `agent/evolution/executor.py`.
  A background evolution pass now requires noninteractive permission for:
  - `fs_write` for workspace memory/knowledge/output edits;
  - `skill_write` for workspace skill repair/creation;
  - `bash` for any helper command the isolated review agent may run.
- `smart-ask`, `always-ask` without remembered grants, and `read-only` skip the
  background pass cleanly with a log entry. `full-access` allows it. This keeps
  Codex-like self-repair available only inside an explicit host boundary and
  prevents another hidden "thinking" wait.
- Added regression coverage:
  - `test_self_evolution_skips_without_noninteractive_permission`
  - Full suite now runs `29` tests.
- Validation:
  - `python -m py_compile agent\evolution\executor.py tests\test_ecorex_web_parallel_backend.py`
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`29` tests).
- Existing v0.1.12 packages became stale after this source change; rebuild the
  desktop runtime, WebUI packages, Linux service tarball, public release zip,
  and manifest hashes before handing a package to the user.

## 2026-06-16 Web Host Boundary And Reconnect Polish

- Parallel read-only review found a conditional P0 host-boundary risk: if WebUI
  was configured to bind a non-loopback address such as `0.0.0.0` while
  `web_password` was empty, `_check_auth()` treated passwordless mode as fully
  authenticated. That could expose message, config, model, and tool permission
  endpoints on a LAN/public bind.
- Added explicit Web host helpers in `channel/web/web_channel.py`:
  - `_effective_web_host()` keeps the local one-click default as `127.0.0.1`
    when no password is configured.
  - `_is_public_bind_host()` treats every non-loopback bind as public.
  - `_validate_web_bind_auth()` refuses to start WebUI on non-loopback hosts
    unless `web_password` is set.
  - `_check_auth()` no longer grants passwordless access if the effective host
    is public.
- Added `test_public_web_bind_requires_password`. The test proves:
  - `web_host=0.0.0.0` plus empty `web_password` raises before startup and is
    not authenticated;
  - default local `127.0.0.1` plus empty password still works for one-click
    local installs;
  - public bind with a configured password can start but still requires login.
- Frontend reconnect polish from Web/Desktop parity review:
  - React no longer displays `"??"` as the missing-time fallback in session
    rows.
  - When a stale stream returns `invalid request_id`, React now loads
    `/api/history` for that session and replaces the cached pending bubble if a
    final assistant message is already persisted. If history has no final
    answer, it still falls back to a paused state.
- Validation:
  - `python -m py_compile channel\web\web_channel.py tests\test_ecorex_web_parallel_backend.py`
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`30` tests).
  - `cd desktop && npm run typecheck` passed.
- Existing v0.1.12 packages became stale again after these changes; rebuild and
  revalidate all hand-test artifacts before user testing.

## 2026-06-16 Final Rebuild After Public-Bind Review

- Rebuilt the renderer after the stale-request recovery change. The WebUI
  static asset for that now-superseded rebuild was `index-BTdIth7N.js` plus
  `index-BG_69rJD.css`.
- `scripts/validate-ecorex-release-artifacts.py` initially caught a real
  packaging regression: the Linux service tarball contained both the new
  `index-BTdIth7N.js` and stale `index-Crsnr3ve.js` under
  `runtime/channel/web/static/app/assets`.
- Root cause: `scripts/prepare-ecorex-web-release.ps1` copied the new
  `desktop/dist` over the copied source tree without clearing the destination
  `static/app` directory first. Desktop runtime staging had the same latent
  risk because it copied source `channel/` without guaranteeing the sidecar
  static app matched `desktop/dist`.
- Fixed packaging scripts:
  - `desktop/scripts/stage-runtime-win.ps1` now clears and copies
    `desktop/dist` into `runtime/channel/web/static/app` during runtime staging.
  - `scripts/prepare-ecorex-web-release.ps1` now clears the runtime Web app dir
    before copying the provided Web build.
  - Source `channel/web/static/app` was regenerated from the current
    `desktop/dist` so a fresh clone does not carry stale assets.
  - `scripts/validate-ecorex-release-artifacts.py` now requires
    `index-BTdIth7N.js` and treats `index-Crsnr3ve.js` as stale.
- Final rebuilt hand-test/package set:
  - Desktop local hand-test package:
    `desktop/release-local-0008/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152986355`
    - SHA256: `D85021734507D87E8D131F91D159895E2326BEB8627AC6E0F62A84C880BC54CC`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72856336`
    - SHA256: `7B837C1EE277EBEF4C064F7A868496BDCECD647126DA0EEB8962A372C9B9F689`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79783963`
    - SHA256: `C40854D86BFAE5074E823B97D7CD057B14B72CB6C669416FFA08FBD1390FCE27`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3101277`
    - SHA256: `8305B467D03B6A32888B09B82B97F06F5850F6E41042DE3A81DF0CBAF98AB481`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `309873075`
    - SHA256: `B8D07FDFCAEC8CB97597732F36390DB241CC4726736F166487E9BBA9BC3357F0`
- Validation:
  - `npm run typecheck` passed.
  - `npm run build` passed and produced `index-BTdIth7N.js`.
  - `npm run stage:runtime:win` passed; staged runtime contains only the new
    WebUI asset and includes `_validate_web_bind_auth` plus
    `_authorize_background_evolution`.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0008 --config.win.signAndEditExecutable=false`
    passed.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12`
    passed.
- Runtime verification:
  - Desktop `release-local-0008` launched on `127.0.0.1:9930`.
  - Freshly extracted WebUI package launched on `127.0.0.1:9931`.
  - Both returned `/api/version == 0.1.12`, served `index-BTdIth7N.js` and
    `index-BG_69rJD.css`, did not serve known old assets, exposed
    `feishu_cli`, `host_diagnostics`, and `browser`, and kept isolated
    permission audit paths.
- Upload/deploy remains paused. Windows signed setup is still pending signing
  provider recovery.

## 2026-06-16 Agent Core Host-Boundary Audit Follow-Up

- User asked whether EcoreX's Agent Core and host capability boundary can be
  aligned more closely with Codex. Two parallel read-only explorer agents
  audited model-visible tools and background execution paths while the main
  thread patched the foreground runtime.
- Root finding: the foreground `AgentStreamExecutor` already had a unified
  permission broker call, but several model-visible or background host
  capabilities either were not classified as dangerous or could bypass the
  foreground executor path:
  - `env_config` can mutate `~/.cow/.env` and hot-reload skills.
  - `send` can expose local files to the active channel/cloud sharing path.
  - `scheduler` can create durable background tasks, and due tasks could later
    call tools without passing through `AgentStreamExecutor`.
  - `evolution_undo` can restore memory/skill files from backups.
  - `web_fetch`, `web_search`, and `vision` perform internet access; `web_fetch`
    can write downloaded documents to `workspace/tmp`; `vision` can upload
    local image bytes to model APIs and may invoke local compression helpers.
  - `host_diagnostics` was read-only in output, but its Feishu status check
    indirectly invoked `lark-cli auth status`.
- Fixes:
  - Added `env_config`, `send`, `scheduler`, `evolution_undo`, `web_fetch`,
    `web_search`, and `vision` to `common/ecorex_tool_permissions.py`.
  - Extended `AgentStreamExecutor` fail-closed fallback so broker exceptions
    block the same dangerous tool set instead of allowing execution.
  - Added direct read-only guards to `env_config`, `send`, `scheduler`,
    `evolution_undo`, `web_fetch`, `web_search`, and `vision` so direct
    invocation paths do not bypass the broker.
  - `env_config` no longer creates `~/.cow/.env` for read-only/list/get paths;
    it only ensures the file exists for `set`.
  - Scheduler foreground mutation is blocked in read-only. Background scheduled
    execution now uses noninteractive `scheduler` authorization, and scheduled
    `tool_call` performs a second noninteractive authorization for the concrete
    target tool, reusing the MCP/browser proxy mapping.
  - `host_diagnostics` now checks noninteractive `feishu_cli` permission before
    running the packaged Feishu CLI status probe; blocked modes return a
    sanitized `blocked` status instead of launching a subprocess.
- Validation:
  - `python -m py_compile common\ecorex_tool_permissions.py agent\protocol\agent_stream.py agent\tools\env_config\env_config.py agent\tools\send\send.py agent\tools\scheduler\scheduler_tool.py agent\tools\scheduler\integration.py agent\tools\evolution_undo\evolution_undo.py agent\tools\web_fetch\web_fetch.py agent\tools\web_search\web_search.py agent\tools\vision\vision.py agent\tools\host_diagnostics\host_diagnostics.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`37` tests).
  - `git diff --check` passed with CRLF warnings only.
- Consequence: every v0.1.12 artifact listed above is now stale again. Rebuild
  desktop runtime, WebUI win/mac packages, Linux service tarball, public release
  zip, manifest hashes, and hand-test launch targets before user testing.

## 2026-06-16 Agent Core Boundary Final Rebuild

- Rebuilt all local v0.1.12 hand-test artifacts after the Agent Core
  host-boundary extension. Renderer assets for this now-superseded rebuild were
  `index-BTdIth7N.js` plus `index-BG_69rJD.css`; packages serving older hashed
  assets are stale.
- Final rebuilt hand-test/package set:
  - Desktop local hand-test package:
    `desktop/release-local-0009/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152990440`
    - SHA256: `B914D1C5DF3A1F9A3C99F2BF87E436CF14BFDBE7780D401ABBB4BAC887BBAF85`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72858380`
    - SHA256: `A76967E93B497A352E7E02A275982A4983B903342CE83661C43D30671363326B`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79780661`
    - SHA256: `44FA3E77C46DA006BF5A9E2911BEB50ED5A10D5D615C0FB427F7615F4DE360A4`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3102645`
    - SHA256: `DFC602AC8269E713077167CB9C89002FE2E7775CBEC2E8402B84FFB000675042`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `309877051`
    - SHA256: `B04C9D1218A4F437DDD9BC9F0B7B9E247F2B768DD4AE020CD89A43310508EB20`
- Validation:
  - `npm run typecheck` passed.
  - `npm run build` passed.
  - `npm run stage:runtime:win` passed.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0009 --config.win.signAndEditExecutable=false`
    passed.
  - `powershell scripts\prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-web-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-public-release.ps1 -Version 0.1.12`
    passed.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12`
    passed; Windows signed setup and macOS DMGs remain intentionally skipped as
    pending external signing/validation.
- Runtime verification:
  - Desktop `release-local-0009` launched from the rebuilt package on
    `127.0.0.1:9899`.
  - Installed WebUI launched from
    `%LOCALAPPDATA%\EcoreX WebUI\runtime` on `127.0.0.1:9909`.
  - Both returned `/api/version == 0.1.12`, served `index-BTdIth7N.js`, and
    exposed `bash`, `feishu_cli`, `host_diagnostics`, `send`, `env_config`,
    `scheduler`, `web_search`, `web_fetch`, `vision`, and `browser`.
  - Permission mode writes/readbacks passed for both runtimes:
    `full-access -> read-only -> full-access`.
  - Desktop audit path:
    `C:\Users\user\AppData\Roaming\ecorex-desktop\permission-audit.jsonl`.
  - WebUI audit path:
    `C:\Users\user\AppData\Local\EcoreX\permissions\permission-audit.jsonl`.
  - Desktop and WebUI ran simultaneously on the same device without observed
    port collision or permission-state collision.
- Upload/deploy remains paused. Windows signed setup is still pending
  SimplySign/Smart Card private-key provider recovery.
## 2026-06-16 SSE Cleanup And MCP Namespace Rebuild

- Parallel explorer review found a real stuck-thinking path in WebChannel:
  request cancel tokens were registered before the worker ran, but successful
  worker completion did not unregister them. A finished request could still
  look active to busy-session/interrupt checks. Fixed WebChannel worker
  finalization so completed requests unregister from the cancel registry while
  preserving the SSE queue until the browser consumes the terminal event.
- The same review found a pre-worker crash path: if `produce(context)` raised
  before the thread-pool callback existed, the SSE stream could keep sending
  keepalives until idle timeout. `_produce_with_session_lock` now sends a
  terminal `done` error event, unregisters the cancel token, and releases the
  session lock through the same finalization path.
- Codex-boundary review found that MCP tools could shadow first-party host
  tools by using names such as `bash`, `browser`, `feishu_cli`, or
  `host_diagnostics`. MCP tools are now exposed to the model as
  `mcp__<server>__<tool>` while preserving the remote tool name internally for
  the actual MCP RPC call. Dynamic MCP sync refuses to replace non-MCP
  first-party tools.
- Permission denial is now a convergence boundary: when the broker denies a
  tool call, the next LLM turn is forced text-only so the model must summarize
  the blocker or ask for authorization instead of repeatedly calling the same
  tool. Chrome DevTools MCP calls also share the `browser:cdp` chain budget.
- Added regression coverage:
  - `test_worker_completion_unregisters_cancel_token_but_keeps_sse_queue`
  - `test_worker_exception_emits_done_and_unregisters_cancel_token`
  - `test_produce_exception_emits_done_and_unregisters_cancel_token`
  - `test_mcp_tool_names_are_namespaced_and_remote_name_is_preserved`
  - `test_sync_mcp_into_agent_does_not_replace_builtin_tool`
  - `test_permission_denial_forces_next_turn_text_only`
  - `test_chrome_devtools_mcp_calls_share_browser_chain_budget`
- Validation:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`48` tests).
  - `python -m py_compile scripts\validate-ecorex-release-artifacts.py channel\web\web_channel.py agent\protocol\agent_stream.py agent\tools\tool_manager.py agent\tools\mcp\mcp_tool.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - `npm run stage:runtime:win` passed.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0011 --config.win.signAndEditExecutable=false`
    passed.
  - `powershell scripts\prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-web-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-public-release.ps1 -Version 0.1.12`
    passed.
  - The release validator now checks WebUI/Linux/Desktop packaged runtime
    source text for the SSE finalization and MCP namespace invariants, and
    `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0011\win-unpacked`
    passed.
- Superseded rebuilt hand-test/package set:
  - Desktop local hand-test package:
    `desktop/release-local-0011/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152993545`
    - SHA256: `BC279B13A14A5FDF3228C1B9FF74CECBD10945C8E492F9D20444787CABA56470`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72859928`
    - SHA256: `78CEDA52A9DFE5CC4504D897810A13DB12946828D6C4DE7D99C7270D539CA1CA`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79780962`
    - SHA256: `F39CBF813C0664C53F5BEC17B9541DE05CA9E35239C725A4854C72144CB8F6D4`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3103890`
    - SHA256: `61A94A2878E7E3432D36A63E322BDA41C0FDEDFCA8FC71F7E2206497DF490866`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `309884116`
    - SHA256: `BFD3F4B8BE6E756F191BD81006E8ABA9C1B8846A3923B17B50D10CFCF41928D0`
- Runtime verification:
  - Old `release-local-0010` desktop and WebUI processes were stopped.
  - Desktop `release-local-0011` launched visibly and served
    `127.0.0.1:9899/app/`.
  - The rebuilt dual Win/Mac WebUI zip was extracted and installed through
    `Install EcoreX WebUI.cmd`; installed WebUI served `127.0.0.1:9909/app/`.
  - Both returned `/api/version == 0.1.12`, served `index-BTdIth7N.js`, exposed
    the expected host-boundary tools, and passed
    `read-only -> full-access` permission readbacks with isolated audit paths.
- Upload/deploy remains paused. Windows signed setup is still pending
  SimplySign/Smart Card private-key provider recovery.
## 2026-06-16 Permission API Regression Hardening

- Added `test_tool_permission_handler_round_trips_mode_and_audit` to cover the
  WebChannel `/api/tool-permissions` handler directly. The test sets
  `read-only`, reads it back through `GET`, switches to `full-access`, reads it
  back again, and verifies that the isolated permission audit file is written.
- Added `test_busy_session_message_interrupts_old_request_and_starts_new_one`
  after parallel audit pointed out that the full `/message` busy-session path
  had only indirect evidence. The test holds a real `SessionLock`, registers an
  old request in the cancel registry, posts a new streamed message, verifies the
  old SSE queue receives `cancelled`, and verifies a new request is created
  instead of returning `session_busy`.
- Added `test_empty_agent_end_emits_done_so_sse_does_not_hang` to prove an
  empty `agent_end` still sends a terminal SSE `done` event. This guards the
  half-output/stuck-thinking class where `chat_channel` would otherwise skip an
  empty reply and leave the frontend waiting for the idle timeout.
- Validation:
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`40` tests).
- Packaging note: this change is test/documentation coverage only. It does not
  change the staged runtime code that was already built into
  `release-local-0009` and the WebUI/Linux/public artifacts listed above.

## 2026-06-16 Global Broker And Desktop Host Boundary Rebuild

- Parallel review found one remaining Agent Core boundary gap: dangerous tools
  were only forced through the broker on Desktop/Web. Non-Web channel runtimes
  could still treat `bash`, `write`, `mcp_server`, and `web_fetch` as
  `not-required`.
- Fixed `ToolPermissionBroker` so dangerous tools are classified globally.
  Non-Web/non-Desktop runtimes now allow explicit `full-access`, deny
  `read-only`, and fail closed immediately for `smart-ask`, `always-ask`, or
  `custom` when no interactive approval surface is available.
- Added `test_non_web_channel_dangerous_tools_still_fail_closed`.
- Desktop host boundary follow-up:
  - Optional capability install now asks the Electron permission manager before
    running installers.
  - Policy preinstall is noninteractive and only proceeds when the local
    permission state allows it.
  - External URL opening now goes through a main-process scheme allowlist:
    only `https:` and `mailto:` are opened; `file:`, `javascript:`, `ms-*`, and
    custom protocols are rejected.
- Validation:
  - `python -m py_compile common\ecorex_tool_permissions.py tests\test_ecorex_web_parallel_backend.py`
    passed.
  - `python tests\test_ecorex_web_parallel_backend.py` passed (`41` tests).
  - `npm run typecheck` passed.
  - `npm run build` passed.
  - `npm run stage:runtime:win` passed.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0010 --config.win.signAndEditExecutable=false`
    passed.
  - `powershell scripts\prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-web-release.ps1 -Version 0.1.12`
    passed.
  - `powershell scripts\prepare-ecorex-public-release.ps1 -Version 0.1.12`
    passed.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12`
    passed.
  - Extended the release validator to inspect
    `desktop/release-local-0010/win-unpacked`: it now extracts Electron
    `app.asar`, checks the staged `resources/ecorex-runtime`, and blocks a
    release if global dangerous-tool permission classification, noninteractive
    fail-closed behavior, Electron capability-install permission gating, or
    external URL scheme allowlisting is missing from the actual package.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0010\win-unpacked`
    passed.
- Superseded rebuilt hand-test/package set:
  - Desktop local hand-test package:
    `desktop/release-local-0010/win-unpacked/EcoreX.exe`
  - `release-artifacts/EcoreX_0.1.12-webui-win-mac.zip`
    - Size: `152990662`
    - SHA256: `ED3A3C17C7498B7086D69B609A93012AB46E390EE9A87359AD5CA868071D4FE8`
  - `release-artifacts/EcoreX_0.1.12-webui-windows-x64.zip`
    - Size: `72858493`
    - SHA256: `38C82185927FC3F86D292252238C682834A162A2D088E87714F6984283D7B325`
  - `release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - Size: `79782566`
    - SHA256: `1E8771375BD09A4638F4A8850D6493B8730D98B08D9579E97D2F254ADB4DDCF4`
  - `release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz`
    - Size: `3102644`
    - SHA256: `CFF1947B3E880203D13CE4334A5D8AE33B008A1D2CF9A4D1C7CCFB0516AD9F8C`
  - `release-artifacts/EcoreX_0.1.12-public-release.zip`
    - Size: `309879158`
    - SHA256: `BD71F0A945ECB8570CCC3FC36FE575C063558CEABE0BD436739159EE1FF621E9`
- Runtime verification:
  - Desktop `release-local-0010` launched visibly and served
    `127.0.0.1:9899/app/`.
  - Installed WebUI launched from the rebuilt dual package and served
    `127.0.0.1:9909/app/`.
  - Both returned `/api/version == 0.1.12`, served `index-BTdIth7N.js`, exposed
    the expected host-boundary tools, and passed
    `full-access -> read-only -> full-access` permission readbacks with
    isolated audit paths.
- Upload/deploy remains paused. Windows signed setup is still pending
  SimplySign/Smart Card private-key provider recovery.

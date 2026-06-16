# EcoreX v0.1.12 Release Manifest

Date: 2026-06-16

## 2026-06-16 Rebuild 0023 Current Candidate

- Current local desktop hand-test artifact:
  - `desktop/release-local-0023/win-unpacked/EcoreX.exe`
  - Unsigned local runnable package for manual testing. The publishable Windows
    NSIS setup still requires signing-provider recovery.
- Current WebUI packages:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,875,304`
    - SHA256 `2168D6F826221DBCD94BDC8F1F8CBC9C4E642A039C846E88E7C444866E9A19F2`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,798,534`
    - SHA256 `CF3B4099B9B7425BA5A8EC976988BF0010E5A8BB75636B03B0AA90B81138AC24`
- Archived/hidden compatibility packages regenerated from the same source:
  - `EcoreX_0.1.12-webui-win-mac.zip`
    - size `153,024,284`
    - SHA256 `358CF5C799206EC6954291CA2B3E76F28E5708E8B6AE2FB79090A69BF173F357`
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
    - size `3,120,581`
    - SHA256 `3323450FE5C4B0FBA117BE2E4717155FFA7AA6570EDBF320F54D2DDCA4E15592`
- Public release ZIP:
  - `EcoreX_0.1.12-public-release.zip`
  - size `154,772,792`
  - SHA256 `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`
- Renderer/static asset for this rebuild:
  - `index-CjBkNLMl.js`
  - `index-BG_69rJD.css`
- Additional source fixes included in 0023:
  - 0022 is stale after the admin image model catalog and release gate were
    tightened. The Admin/Models image capability now surfaces
    `gpt-image-2-pro` as the OpenAI default instead of suggesting the old
    `gpt-image-2` path.
  - Official built-in workspace skill copies are refreshed from packaged
    `skills/` when they miss release-critical markers. This fixes the case
    where `~/EcoreX/skills/image-generation` from an older release masked the
    new `gpt-image-2-pro` default.
  - Explicit same-name built-in overrides are preserved only when the workspace
    skill contains `.ecorex-custom-override`, which `SkillService` writes for
    intentional `allow_builtin_override=true` installs.
  - OpenAI image generation keeps `gpt-image-2-pro` as the default, omits
    `response_format`, uses `/images/generations` only when no input image is
    provided, and uses `/images/edits` with multipart `image` / `image[]` for
    edit/reference/local input image or `image_url` requests.
- Validation passed:
  - `python -m unittest tests.test_ecorex_web_parallel_backend` (`79` tests).
  - Targeted OpenAI image tests cover both pro fallback and edit endpoint
    routing, including no-image `/images/generations`, single-image multipart
    `image`, and multi-image multipart `image[]`.
  - `npm run typecheck`, `npm run build`, `npm run stage:runtime:win`.
  - Electron dir package to `desktop/release-local-0023/win-unpacked`.
  - WebUI/Linux/public packaging.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0023\win-unpacked`.
  - Runtime smoke ran desktop `http://127.0.0.1:9899/app/` and installed WebUI
    `http://127.0.0.1:9909/app/` simultaneously. Both report version
    `0.1.12`, serve `index-CjBkNLMl.js` / `index-BG_69rJD.css`, expose
    `feishu_cli` / `host_diagnostics` / `browser` / `bash`, return empty
    `/api/active-requests`, and round-trip `read-only -> full-access` tool
    permission modes.
  - Packaged desktop runtime, installed WebUI runtime, and
    `C:\Users\user\EcoreX\skills` copy of `image-generation` all default to
    `gpt-image-2-pro`; text-only/no-input-image requests route to
    `/images/generations`, edit/reference/local input image or `image_url`
    requests route to `/images/edits` multipart `image` / `image[]`, and the
    OpenAI branch contains no `response_format`.
  - Real WebUI runtime OpenAI image smoke passed with the admin-configured
    backend and explicit `model="gpt-image-2-pro"`: elapsed `22.74s`, reported
    model `gpt-image-2-pro`, output
    `C:\CowAgent\release-artifacts\image-smoke-0022-explicit-pro\09a2a7232e4e.png`
    (`768,607` bytes). This smoke did not print or persist API credentials.
  - Download page browser render smoke passed after the cache-buster refresh:
    `index.html` loads `./site.js?v=0.1.12-0023`, renders four cards
    (`Windows`, `macOS`, `Windows WebUI`, `macOS WebUI`), has no broken images,
    and the public ZIP includes the required PNG assets plus only the ready
    Windows/macOS WebUI downloads.

## 2026-06-16 Rebuild 0021 Source Delta

- `release-local-0020` and its WebUI/public artifacts are stale after the
  0021 source changes. Rebuild the next hand-test set as `release-local-0021`
  before manual testing, upload, or deployment.
- Source fixes included in the pending 0021 candidate:
  - Default filesystem fallback no longer includes `web_file_serve_root` or
    Home as a generic read/write root; default no-profile access is
    workspace/cwd scoped.
  - `/api/file` defaults to workspace/upload preview roots and still passes
    through `authorize_file_access("read", ...)`.
  - Log streaming reuses the host-diagnostics log tail path for permission
    checks and masking.
  - Memory index sync, `MemoryService`, and `memory_get` now obey filesystem
    read profiles before indexing/listing/reading memory or knowledge files.
  - `feishu_cli` `authRequired=true` / `available=false` results force the
    next LLM turn text-only, so EcoreX must ask the user to finish Feishu
    authorization/setup instead of continuing through `bash`.
  - OpenAI image generation defaults to `gpt-image-2-pro`, uses official GPT
    Image parameters, omits `response_format`, and falls back to
    `gpt-image-2` only for model/access unavailability. Local connectivity is
    still unverified because no `OPENAI_API_KEY` is configured on this machine.
  - Download page UTF-8 text, image fallback behavior, image-asset validator
    checks, and final grid filtering were restored. The public grid now targets
    Windows desktop, macOS DMG choices, Windows WebUI, and macOS WebUI.
- Validation already passed before packaging:
  - `python -m unittest tests.test_ecorex_web_parallel_backend` (`74` tests).
  - `python -m py_compile` for the changed runtime and validator files.
  - `node --check deploy\ecorex-site\site.js`.
  - Local image decode check for `deploy/ecorex-site/assets/icon.png`,
    `ecorex-app-preview.png`, and `ecorex-ecosystem-hub.png`.
- Manifest state before the 0021 artifact refresh:
  - `webui-windows-x64` and `webui-macos-universal` remain public ready entries
    until their 0021 size/hash are refreshed.
  - `webui-win-mac` and `web-linux-service` are archived/hidden and must not be
    included in the final public download ZIP unless the release plan changes.

## 2026-06-16 Agent Host Boundary Rebuild 0020

- Historical local desktop hand-test artifact for this 0020 rebuild:
  - `desktop/release-local-0020/win-unpacked/EcoreX.exe`
  - This is an unsigned local runnable package for manual testing. The
    publishable Windows NSIS setup still requires signing-provider recovery.
- Historical local WebUI dual-platform package for this 0020 rebuild:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `153,010,369`
  - SHA256 `89FA0E92BFEE3087545714EF8CE7583C45D12106C63C6CEAB482962F0118BFB9`
- Hidden compatibility packages:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,868,347`
    - SHA256 `020D6F30101FB5304CD6BE4C92DB1BB2A2CDFB5B426FC3240877A3871B5A7C5A`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,792,191`
    - SHA256 `294F94C9E1FFAE62845A2CC498C19595C076E7DBC7D48CAB12E6C13D8337AEEA`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,114,626`
  - SHA256 `10AB3F55BA5D60158EA4D1B2AE9B0D61CA1207630F5CA5C3986A4F1207D40927`
- Public release zip:
  - `EcoreX_0.1.12-public-release.zip`
  - size `309,931,864`
  - SHA256 `E1E90D76B94FE289140F7C2411CD55456A2FF71CD9C55B7909AAECE38BE04B28`
- Renderer/static asset for this rebuild:
  - `index-CjBkNLMl.js`
  - `index-BG_69rJD.css`
- Key closure items since 0018/0019:
  - Automatic memory persistence now obeys the shared filesystem profile before
    creating/writing `MEMORY.md`, daily memory files, user memory files, and
    Deep Dream diary files.
  - Knowledge list/read/graph paths now obey the same filesystem profile before
    reading `knowledge/*.md`.
  - `web_fetch`, `web_search`, and `vision` fail closed if the permission
    broker is unavailable.
  - WebUI permission state now follows `config.appdata_dir` when
    `ECOREX_USER_DATA` / `ECOREX_DESKTOP_USER_DATA` is not set. This prevents
    extracted or installed WebUI runtimes from sharing a stale global
    `%LOCALAPPDATA%\EcoreX\permissions` state by accident.
  - The release validator now requires packaged runtime sentinels for memory,
    knowledge, network/vision fail-closed behavior, and appdata-backed
    permission state.
- Validation passed:
  - `python tests\test_ecorex_web_parallel_backend.py` (`64` tests).
  - `npm run typecheck`, `npm run build`, `npm run stage:runtime:win`.
  - Electron dir package to `desktop/release-local-0020/win-unpacked` using
    the local `node_modules/electron/dist` cache after GitHub/mirror downloads
    repeatedly timed out/reset.
  - WebUI/Linux/public packaging scripts.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0020\win-unpacked`.
- Runtime smoke:
  - Desktop `127.0.0.1:9946` and freshly extracted WebUI
    `127.0.0.1:9947` ran simultaneously.
  - Both returned version `0.1.12`, served `index-CjBkNLMl.js` and
    `index-BG_69rJD.css`, exposed `feishu_cli`, `host_diagnostics`, and
    `browser`, and returned `/api/active-requests`.
  - Packaged WebUI runtime permission smoke passed without env overrides:
    permission audit path resolved to
    `release-artifacts/manual-test-webui-0020/state/appdata/permissions/permission-audit.jsonl`,
    `read-only` blocked local writes, and `full-access` allowed writes.
  - Packaged WebUI runtime memory/knowledge smoke passed with a custom
    filesystem profile: blocked memory paths did not create files, daily memory
    creation raised `PermissionError`, `knowledge/secret.md` was blocked and
    hidden from the tree, while `knowledge/public.md` stayed readable.
  - Packaged WebUI CDP MCP startup reached `chrome-devtools` ready state with
    29 model-visible `mcp__chrome-devtools__*` tools.
- Boundary truth for manual test:
  - EcoreX v0.1.12 is still `Codex-boundary-inspired`, not Codex-equivalent.
    That 0020 package aligned at policy/routing/current-process liveness and
    tool-level filesystem profile enforcement. Full parity still requires
    durable turn/process APIs, replayable run logs after restart, product
    subagents, patch/worktree transactions, editable filesystem/network
    profiles, and OS/container sandboxing.
- Anything built before `release-local-0020` was stale for that hand-test
  cycle. Upload/deploy/GitHub sync remains paused until user manual testing
  passes.

## 2026-06-16 Filesystem Profile And Desktop Bridge Rebuild 0018

- Historical local desktop hand-test artifact for this 0018 rebuild:
  - `desktop/release-local-0018/win-unpacked/EcoreX.exe`
  - This is an unsigned local runnable package for manual testing. The
    publishable Windows NSIS setup still requires signing-provider recovery.
- Historical local WebUI dual-platform package for this 0018 rebuild:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `153,008,347`
  - SHA256 `11F52C262AF951A1C710A8F6AC6B23CC81A106D4CDE332F03867F350DB2E001C`
- Hidden compatibility packages:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,867,331`
    - SHA256 `CF0E70D419EACE4E452F8871C31B1708477F73A6F0522549F35809C1989CD92A`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,792,418`
    - SHA256 `C59AA5E8DE5281CDE5043850FB98A6557E842D00581CD16D96BF2E1FCE42B5A1`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,113,664`
  - SHA256 `B89913E4DED205C6912C04A2A5172638736E86C2A5846C3BA7863171B669EFF1`
- Public release zip:
  - `EcoreX_0.1.12-public-release.zip`
  - size `309,927,039`
  - SHA256 `E3F38AF9898CC091055FB2E37422ACD772BE06AC041ECFA539A19181ECC36A76`
- Renderer/static asset for this rebuild:
  - `index-CjBkNLMl.js`
  - `index-BG_69rJD.css`
- Key closure items since 0017:
  - File tools `read`, `ls`, `write`, and `edit` now call the shared
    filesystem permission broker.
  - `send` now also calls `authorize_file_access("read", ...)`, so local file
    sending cannot bypass custom filesystem profiles.
  - `/api/file` now shares the same `authorize_file_access("read", ...)`
    decision layer as agent file tools.
  - Desktop bridge now allows `GET /api/active-requests`, so renderer runtime
    snapshots can receive backend active-request state instead of silently
    falling back to an empty list.
  - Electron `PermissionManager` reloads `permissions.json` on each state read
    instead of serving stale cached mode/grant data after Python broker writes.
  - `custom` mode without a filesystem profile fails closed; explicit
    filesystem profiles support workspace roots and deny globs.
  - The release validator checks packaged WebUI/Linux/Desktop runtime source
    sentinels for the filesystem profile layer and file-tool hooks.
- Validation passed:
  - `python tests\test_ecorex_web_parallel_backend.py` (`61` tests).
  - `npm run typecheck`, `npm run build`, `npm run stage:runtime:win`.
  - Electron dir package to `desktop/release-local-0018/win-unpacked`.
  - WebUI/Linux/public packaging scripts.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0018\win-unpacked`.
- Runtime smoke:
  - Desktop `127.0.0.1:9942` and freshly extracted WebUI
    `127.0.0.1:9943` ran simultaneously.
  - Both returned version `0.1.12`, served `index-CjBkNLMl.js`, exposed
    `feishu_cli`, `host_diagnostics`, and `browser`, returned
    `/api/active-requests`, and passed permission mode round-trip.
  - Packaged WebUI runtime file tools, `send`, and running `/api/file` passed
    custom filesystem profile smoke.
- Boundary truth for manual test:
  - EcoreX v0.1.12 is Codex-boundary-inspired and now has a first filesystem
    profile enforcement layer. It is still not a full Codex host clone until
    user-facing filesystem profile editing, network profiles, durable
    process/turn APIs, replayable run logs, product sub-agents, and
    patch/worktree transactions exist.
- Anything built before `release-local-0018` was stale for that hand-test
  cycle.

## 2026-06-16 Agent Core Boundary Rebuild 0016

- Historical local desktop hand-test artifact for this 0016 rebuild:
  - `desktop/release-local-0016/win-unpacked/EcoreX.exe`
  - This is an unsigned local runnable package for manual testing. The
    publishable Windows NSIS setup still requires signing-provider recovery.
- Historical local WebUI dual-platform package for this 0016 rebuild:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `153,003,127`
  - SHA256 `16CAC376CDEA29BCAA73808A8CE404EF92017EBAD1981EA306A6CDDED336CA06`
- Hidden compatibility packages:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,864,722`
    - SHA256 `8BB114AED0A9D8AC6ADF827F67ACE3765CBE92AD85B20764F1CC4A35DCECCC44`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,793,151`
    - SHA256 `9E4DF4098BDEAF48C93D54949B473BEC971C90BEDF335B7086EC38A0F2AF6BE7`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,111,057`
  - SHA256 `620704EAC2D2DA1DD7C57E6A6BEB003353AC811720C7246D6AA54A5993F9BBAB`
- Public release zip:
  - `EcoreX_0.1.12-public-release.zip`
  - size `309,912,731`
  - SHA256 `D406F1C6ABB07A0EC9DBEE239D936BC4D5FDA48FCBD26DDB28CB4CE69D9E300B`
- Renderer/static asset for this rebuild:
  - `index-CjBkNLMl.js`
  - `index-BG_69rJD.css`
- Key closure items since 0015:
  - Simple raw Feishu shell coverage now includes `npx @larksuite/cli...` and
    `node .../cli-main/scripts/run.js`, not only `lark-cli` / `npx lark-cli`.
  - The same-request SSE broadcast/replay fix is verified inside both the
    packaged desktop runtime and the installed WebUI runtime.
  - Runtime smoke ran desktop on `127.0.0.1:9938` and installed WebUI on
    `127.0.0.1:9939` simultaneously. Both returned version `0.1.12`, served
    `index-CjBkNLMl.js`, exposed `feishu_cli`, `host_diagnostics`, and
    `browser`, returned `/api/active-requests`, and passed permission mode
    round-trip `read-only -> full-access`.
  - Packaged desktop and WebUI Python runtimes both passed same-request SSE
    broadcast smoke. Packaged desktop and WebUI runtimes both passed raw
    `npx @larksuite/cli...` -> `feishu_cli` autoroute smoke; desktop also
    covered `node C:/cli-main/scripts/run.js ...` and `auth login`.
  - `python scripts\validate-ecorex-release-artifacts.py --version 0.1.12 --desktop-dir desktop\release-local-0016\win-unpacked`
    passed. It validated ready artifact hashes, public zip contents, WebUI
    static references, and packaged desktop host-boundary sentinels.
- Boundary truth for manual test:
  - EcoreX v0.1.12 is now Codex-boundary-inspired at the policy/routing/current
    process liveness layer. It is not a full Codex host clone yet. Future work
    still needs durable process/turn APIs, replayable run logs after restart,
    product-level sub-agents, patch/worktree transactions, and full sandbox
    profiles.
- Anything built before `release-local-0016` was stale for that hand-test
  cycle.

## 2026-06-16 Same-Request SSE Broadcast Rebuild

- Historical local desktop hand-test artifact for this 0015 rebuild:
  - `desktop/release-local-0015/win-unpacked/EcoreX.exe`
  - This is an unsigned local runnable package for manual testing. The
    publishable Windows NSIS setup still requires signing-provider recovery.
- Historical local WebUI dual-platform package for this 0015 rebuild:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `153,002,341`
  - SHA256 `3A5F83F8772B3524F84CFB5C179522B0E06AD15647F30F1040A0617904EBF313`
- Hidden compatibility packages:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,864,331`
    - SHA256 `FCA73F5C782683FCBDC69ED1AB4C527B2746DB0DBE29A12502C6D8F31FECFB09`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,791,338`
    - SHA256 `2A10E3E36D206FF4BF55268294D17DB1341DF3D8FB0A8CD2F2826A2F18827573`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,110,677`
  - SHA256 `20EF1A0C8E6B250FD4EFD2B3F44FABC8C4611E1DAE2A02346FA01A0FEA4F55B7`
- Public release zip:
  - `EcoreX_0.1.12-public-release.zip`
  - size `309,908,092`
  - SHA256 `045240D7BA63EF543F9D97920149F308F8DDC8B3C07F952A7BD7FEF9227F1ACE`
- Renderer/static asset for this rebuild:
  - `index-CjBkNLMl.js`
  - `index-BG_69rJD.css`
- Key additional closure items:
  - `FeishuCli.apply_config()` and `HostDiagnostics.apply_config()` refresh
    cached runtime fields after ToolManager/AgentInitializer config merges.
  - `feishu_cli action=ensure` now respects `auto_install=false` and explicit
    `install_if_missing=false`.
  - Skill load diagnostics are model-visible through the skills prompt and
    `host_diagnostics`, so malformed/missing skills do not silently disappear.
  - Renderer accepts `voice_attach` post-`done` tail events, restores
    `extras.audio` from history, and treats `/uploads/...` as runtime HTTP
    media instead of a local filesystem path.
  - Release validation now reads actual packaged `index.html` references, not
    only archive file existence, and the public zip cannot contain extra stale
    download files.
  - Simple raw `bash lark-cli ...` / `npx lark-cli ...` tool calls now
    autoroute into `feishu_cli`; complex raw shell forms still hard-stop.
  - Same-request SSE now uses replay/broadcast logs with per-subscriber
    cursors so desktop and WebUI can subscribe to the same live request without
    stealing each other's stream events.

## 2026-06-15 Formal Package Rebuild

- Public download page remains the target at `https://www.ecoreai.cn/ecorex-agent/`.
  These artifacts are locally rebuilt for manual testing first; upload/deploy
  only after the user confirms the formal packages are good.
- Windows desktop installer is pending re-sign after the final log-masking
  rebuild:
  - `EcoreX_0.1.12_x64-setup.exe`
  - the hand-test `win-unpacked` runtime for that pass was rebuilt locally.
  - the previous signed setup is not publishable as the v0.1.12 artifact.
  - `signtool /debug` currently reaches `After Private Key filter, 0 certs were left`; wait for the Certum/SimplySign private-key provider to recover before generating the final signed setup.
- Local WebUI is prepared as one dual-platform package:
  - `EcoreX_0.1.12-webui-win-mac.zip`
  - size `152,993,545`
  - SHA256 `BC279B13A14A5FDF3228C1B9FF74CECBD10945C8E492F9D20444787CABA56470`
- Hidden compatibility packages remain publishable:
  - `EcoreX_0.1.12-webui-windows-x64.zip`
    - size `72,859,928`
    - SHA256 `78CEDA52A9DFE5CC4504D897810A13DB12946828D6C4DE7D99C7270D539CA1CA`
  - `EcoreX_0.1.12-webui-macos-universal.tar.gz`
    - size `79,780,962`
    - SHA256 `F39CBF813C0664C53F5BEC17B9541DE05CA9E35239C725A4854C72144CB8F6D4`
- Linux/web deployment package:
  - `EcoreX_0.1.12-web-linux-service.tar.gz`
  - size `3,103,890`
  - SHA256 `61A94A2878E7E3432D36A63E322BDA41C0FDEDFCA8FC71F7E2206497DF490866`
- Public release zip prepared only as a pending-Windows validation bundle:
  - `EcoreX_0.1.12-public-release.zip`
  - size `309,884,116`
  - SHA256 `BFD3F4B8BE6E756F191BD81006E8ABA9C1B8846A3923B17B50D10CFCF41928D0`
  - excludes Windows desktop because `windows-x64` is `pending-signature`.
- WebUI `/chat` now serves the same desktop-style React app as `/app/`.
- Local WebUI proxies `/client/*` to the Admin API client endpoints and no longer relies on a local fallback account once the backend is reachable.
- Packaging and release pitfalls from this rollout are captured in `docs/ecorex/packaging-release-guidelines.md`.

## 2026-06-15 WebUI/Desktop Parity Follow-up

- This is a same-version v0.1.12 hardening release. GitHub upload and release
  asset refresh happen only after the formal local WebUI and desktop builds pass
  the checks below.
- CDP is now enforced as the default first browser-control path even when an installed WebUI has a minimal `config.json`; `tools.browser` and `chrome-devtools` MCP defaults are repaired at runtime and generated by the Win/Mac WebUI package script.
- The core runtime includes the Python `playwright` package as the CDP client dependency, while browser-kernel installation remains limited to the Chromium fallback capability pack.
- Session rows no longer display message counts such as `23 条`; stale cached pending assistant messages are normalized to an explicit paused state.
- Windows shell timeouts now kill the launched process tree so long PowerShell scans cannot keep WebUI or desktop sessions permanently in `思考中`.
- First launch now defaults to dark mode on WebUI and desktop. Dark-mode primary text uses high-contrast white and scrollbars follow theme variables.
- The composer now exposes local access modes from a single Codex-style upward menu, with the permission indicator and token/context usage on one horizontal footer line; the backend permission broker honors `full-access` for local shell/browser tools while preserving read-only blocking.
- Switching sessions, refreshing the page, or temporarily detaching SSE no longer cancels active work. The renderer persists `request_id` on live assistant messages and reconnects to `/stream?request_id=...`; explicit Stop and same-session interrupt sends are the cancellation boundaries.
- If the local runtime has fully exited and a cached `request_id` is no longer valid, the renderer first reloads session history and replaces the cached pending bubble when a final assistant answer is already persisted; otherwise it normalizes the bubble to a paused state instead of leaving `思考中` or showing a raw `invalid request_id`.
- The composer top divider was removed for both WebUI and desktop; `composer-zone` must not restore a `border-top`.
- Tool streams include `tool_call_id` and the renderer matches repeated `bash`/`browser` calls by id, preventing stale running tool rows.
- The context meter now counts tool arguments/results, reasoning/phase content, files, and image/video references instead of only raw message text length.
- WebUI and desktop message rendering now share local file/image preview behavior for Markdown `file://` links, Windows paths, and macOS absolute paths.
- WebUI active-session sends now interrupt the previous run and retry after the session lock is released instead of surfacing `session_busy`.
- Shared chat bubbles now include an icon-only one-click copy action for message text.
- Desktop and WebUI same-device coexistence is hardened: desktop defaults to the EcoreX workspace, runtime memory storage follows `agent_workspace`, and installation manifests distinguish `desktop` from `webui`.
- Theme scrollbars now follow the active light/dark palette.
- Detailed notes and regression checks are recorded in `docs/ecorex/v0.1.12/webui-desktop-parity-goal.md`.

## Scope

- Desktop UX fixes: session switching clears stale transcript/composer state immediately; model selector shows the active model and no longer opens Settings; session pin/rename/delete actions are hover-only; stop/cancel can close an in-flight assistant bubble even before a backend request id arrives; failed tool details stay collapsed by default; dark mode now updates the Windows title bar; the jump-to-latest control is centered/larger; expanded long answers place the collapse button below the full reply.
- Composer telemetry: the composer now shows compact daily and weekly token usage meters, plus an estimated current-context meter under the input. The default context threshold is `258k`; compacting still follows the existing threshold logic.
- Browser automation: CDP is the first browser automation path, auto-launching Chrome/Edge on `http://127.0.0.1:9222` with Playwright fallback. The packaged default registers `chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222 --no-usage-statistics`.
- Agent host boundary: EcoreX now exposes model-visible `host_diagnostics`
  and `feishu_cli` tools, noninteractive MCP permission checks, CDP/MCP
  endpoint alignment, recursive secret masking, and tool-chain convergence
  guards so the agent can inspect host state and self-correct before falling
  back to repeated raw shell calls.
- Browser loop fix: browser click/wait/press tool results now include a bounded post-action snapshot so the agent can observe refreshed pages instead of staying in a "thinking" loop after the page already answered.
- Admin console: token totals and limits use compact `k/m` labels with exact values in hover titles; usage statistics only include active, non-deleted EcoreX users; the error traceback view defaults to failures only and no longer stores warn/info/success client events in `error_logs`; the sidebar brand mark no longer depends on a possibly missing image.

## Version Sources

- Desktop package: `desktop/package.json` and `desktop/package-lock.json` are `0.1.12`.
- Admin API: `deploy/ecorex-admin-api/ecorex_admin_api.py` reports `0.1.12`.
- Desktop enterprise policy default client event key: `ecorex-desktop-v0.1.12`.
- Download page production deployment remains paused until the Windows setup is
  re-signed and the user completes manual testing.

## Windows Artifact

- Current runnable local-test artifact:
  `desktop/release-local-0023/win-unpacked/EcoreX.exe`.
- Current desktop hand-test URL: `http://127.0.0.1:9899/app/`.
- `desktop/release/win-unpacked/EcoreX.exe` may still be an older
  running manual-test process. If it is open, Electron Builder cannot overwrite
  it and browser checks against its sidecar can still show old hashed JS.
- Windows setup artifact: pending re-sign.
- Previous signed setup `desktop/release/EcoreX_0.1.12_x64-setup.exe` must not
  be uploaded as the v0.1.12 current artifact after the log-masking rebuild.
- Blockmap: not copied to `release-artifacts`; the Electron Builder blockmap
  was generated before Authenticode signing and must not be advertised until
  updater validation is done against the signed installer.
- Authenticode status: pending. `signtool /debug` sees the certificate by hash
  but fails at the private-key filter, so the final NSIS setup cannot be
  generated until the signing provider is available again.
- Recovery check: run `npm run sign:win:preflight` from `desktop/` before the
  next signed packaging attempt. It must pass before `package:win:signed`.
- Build note: runtime staging bundled `C:\cli-main\bin\lark-cli.exe` into
  `resources/ecorex-runtime/tools/bin/lark-cli.exe`; `config-template.json`
  includes `tools.feishu_cli`, `host_diagnostics`, and chrome-devtools MCP
  `--browserUrl` defaults. The template `character_desc` is ASCII-only so
  Windows PowerShell 5 default ANSI decoding cannot corrupt JSON parsing.
- GitHub source commit: `b3d1bbf292f00c775e051ea6f5e0657c06cc0957` initially published v0.1.12. A follow-up v0.1.12 hardening sync updates the same tag/release assets after the sub-agent review fixes.
- GitHub Release: `https://github.com/zhangyifanjackson-dotcom/EcoreX/releases/tag/v0.1.12`
- Note: the repository/release may require an authenticated GitHub session; anonymous API checks can return 404 if the repo is private.

## Verification

- `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py`
- `node --check deploy/ecorex-site/admin/admin.js`
- `npm run typecheck`
- `npm run build`
- `python -m py_compile` for changed runtime files including
  `agent_stream.py`, `mcp_client.py`, `mcp_tool.py`, `feishu_cli.py`,
  `host_diagnostics.py`, `ecorex_tool_permissions.py`, `config.py`, and
  `web_channel.py`.
- Runtime staging: `desktop/scripts/stage-runtime-win.ps1`.
- Packaging:
  - `electron-builder --win --dir` to default `release/win-unpacked` was blocked
    by the currently running old EcoreX process.
  - `electron-builder --win --dir --publish never --config.directories.output=release-local-0023 --config.win.signAndEditExecutable=false`
    produced the current runnable local-test package after the 0023 fixes. This
    skips executable resource editing and is for hand testing only, not final
    signed installer publication.
  - Final formal Windows packaging still requires signing preflight recovery,
    then a clean NSIS rebuild and Authenticode verification.
- `scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.1.12`
- `scripts/prepare-ecorex-web-release.ps1 -Version 0.1.12` was rerun after the
  2026-06-16 public-bind, stale-asset, and Linux static-copy fixes and
  produced current web-linux SHA256
  `3323450FE5C4B0FBA117BE2E4717155FFA7AA6570EDBF320F54D2DDCA4E15592`.
- `scripts/prepare-ecorex-public-release.ps1 -Version 0.1.12` was rerun after
  updating `deploy/ecorex-site/manifest.json` to the current 0023 WebUI/Linux
  hashes and produced public zip SHA256
  `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`.
- `prepare-ecorex-public-release.ps1` now writes `checksums.json` as UTF-8
  without BOM. Strict Python JSON parsing of `site/manifest.json` and
  `checksums.json` inside the zip passed.
- WebUI package structure validation, macOS installer `bash -n`, Linux service
  package check, and public-release zip validation passed.
- Current installed WebUI launch used `127.0.0.1:9909`; `/app/` served
  `index-CjBkNLMl.js` / `index-BG_69rJD.css`, `/api/tools` exposed
  `feishu_cli`, `host_diagnostics`, `browser`, and `bash`, and permission mode
  writes/readbacks succeeded for `read-only -> full-access`.
- Current local desktop hand-test launch uses
  `desktop/release-local-0023/win-unpacked/EcoreX.exe` on
  `127.0.0.1:9899`; `/api/version` returned `0.1.12`, `/app/` served
  `index-CjBkNLMl.js` / `index-BG_69rJD.css`, `/api/tools` exposed
  `feishu_cli`, `host_diagnostics`, `browser`, and `bash`, and permission mode
  writes/readbacks succeeded for `read-only -> full-access`.
- Current installed WebUI hand-test launch uses `http://127.0.0.1:9909/app/`.
  Desktop `9899` and WebUI `9909` were verified running simultaneously on one
  device with isolated permission audit paths, so the current local hand-test
  pair has no observed port or permission-state collision.
- Packaged runtime smoke passed for both desktop and installed WebUI Python:
  a fake `bash` tool call containing `lark-cli docx +read --as user` was
  autorouted to `feishu_cli` with `action=run`, preserved timeout, and returned
  `reroutedFrom=bash:raw bash lark-cli`.
  The same smoke also opened two stream generators for one `request_id` and
  verified both received the same `id: 0` terminal `done` event.
- P1 host-boundary fixes are present in the staged desktop/WebUI/Linux
  runtimes: trusted default CDP MCP signature checking, SkillService
  `skill_write` noninteractive authorization, Bash timeout normalization,
  Browser worker isolation after cancel/timeout, MCP stdio process-tree
  shutdown, and streamable-http SSE total deadline.
- The release validator was extended and rerun with
  `--desktop-dir desktop\release-local-0023\win-unpacked`; it now inspects the
  packaged Electron `app.asar` and `resources/ecorex-runtime` for global
  dangerous-tool permission classification, noninteractive fail-closed behavior,
  Electron capability-install/preinstall permission gating, and external URL
  scheme allowlisting. The validator also checks packaged WebUI/Linux/Desktop
  runtime source text for request finalization, permission-denial convergence,
  MCP namespace isolation, and Chrome DevTools MCP browser-chain budgeting.
- Public release zip local install smoke passed in a temporary directory with
  `CHECK_PUBLIC=0 CHECK_CADDY=0`; ready artifacts matched manifest size/SHA,
  while `windows-x64`, `macos-arm64-dmg`, and `macos-x64-dmg` were correctly
  skipped as pending.
- Already-running installed WebUI/Desktop processes may continue serving old
  hashed JS until restarted or replaced. This must be checked during manual
  testing before judging the rebuilt package.
- Windows setup signing is pending. The current `release-local-0023`
  `win-unpacked` runtime can be
  used for local functional testing, but the publishable NSIS setup must be
  rebuilt and signed after the Certum/SimplySign private-key provider is
  available again.

## Deployment Boundary

- Deploy/upload remains paused until the user completes manual testing of the
  freshly rebuilt formal packages.
- After confirmation, update the production Admin Web/API, download files,
  `/srv/ecorex-agent-download/current`, GitHub source, and release assets in one
  coordinated v0.1.12 sync.
- GitHub source sync and release upload should include this manifest, the host
  boundary audit, and the packaging guidelines so a fresh clone can resume from
  the same state.

## 2026-06-16 Signed Windows And Web Deployment

- Shared renderer change:
  - Desktop and WebUI message rendering now linkifies Markdown links, bare
    `http(s)` URLs, `file://` URLs, Windows drive paths, UNC paths, and common
    macOS/Linux absolute paths.
  - Desktop path clicks use the Electron `openPath` bridge.
  - WebUI path clicks use `/api/file?path=...`.
  - Electron external URL allowlist is `http:`, `https:`, and `mailto:`.
- Current renderer hash:
  - `index-B_LYG2V7.js`
  - `index-BG_69rJD.css`
  - `index-CjBkNLMl.js` is stale for this release pass.
- Current signed Windows setup:
  - Path: `desktop/release/EcoreX_0.1.12_x64-setup.exe`
  - Published copy: `release-artifacts/EcoreX_0.1.12_x64-setup.exe`
  - Size: `149,102,488`
  - SHA256: `DC692944B4F049D8273443A0E4D56039FC0409611B42A8411488CCE76E24B728`
  - Authenticode: `Valid`
  - Signer: `CN=Zhang Yifan` / Certum Code Signing 2021 CA
  - Timestamp: DigiCert timestamp responder
- Current WebUI/Web artifacts:
  - Windows WebUI ZIP:
    `EcoreX_0.1.12-webui-windows-x64.zip`, size `72,875,633`,
    SHA256 `3669213EE57789983C444D6F46943306B3D18CE267522BE1E5DD94AA20F6BD05`.
  - macOS WebUI tarball:
    `EcoreX_0.1.12-webui-macos-universal.tar.gz`, size `79,797,578`,
    SHA256 `E4BE9D23954FDDAD94ACE1F52432673C184792A5D254CBF6649E11641FC8F1B7`.
  - Web Linux service tarball:
    `EcoreX_0.1.12-web-linux-service.tar.gz`, size `3,121,437`,
    SHA256 `45393EBB267BA36AD11F9CA9EECC550E51B68BF32B6E737D4AD63E940F3BADA9`.
- Current public release ZIP:
  - Path: `release-artifacts/EcoreX_0.1.12-public-release.zip`
  - Size: `303,802,531`
  - SHA256: `A23E1ABF12A6ADBE25C560EC3AB4E75521CC06624E0EBCC31EB8E4E497CCC55B`
  - Validation: `scripts/validate-ecorex-release-artifacts.py --version 0.1.12`
    passed through `prepare-ecorex-public-release.ps1`.
- Production download page:
  - Public URL: `https://www.ecoreai.cn/ecorex-agent/`
  - Current symlink: `/srv/ecorex-agent-download/releases/20260616053539-v0.1.12`
  - `check-ecorex-server-release.sh` passed after deploy.
  - Public HTTP checks passed for manifest, root, static assets, admin auth
    gate, client policy gate, Windows installer, Windows WebUI, and macOS
    WebUI.
- Production WebUI runtime:
  - Current symlink: `/opt/ecorex-web/releases/20260616053616-v0.1.12`
  - Public `/app/` serves `index-B_LYG2V7.js`.
  - Root `check-ecorex-web-release.sh` passed including authenticated login,
    app, auth-check, version, and SSE checks.
  - Production host now has Node.js `v22.22.3` and npm/npx `10.9.8`; after
    restart, `chrome-devtools` MCP initialized with 29 namespaced tools.
- macOS desktop DMGs:
  - Still `pending-validation`.
  - Correct build path is `.github/workflows/ecorex-desktop-release.yml` on a
    `macos-15` runner.
  - Do not publish or rename old v0.1.11 DMGs as v0.1.12.

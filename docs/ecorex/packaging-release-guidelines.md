# EcoreX 打包发布准则

## 2026-06-16 v0.1.12 Release Gate Addendum 0023

- Current v0.1.12 hand-test source of truth is `release-local-0023`. Anything
  before `desktop/release-local-0023/win-unpacked/EcoreX.exe` is stale for
  Current/Latest release notes, manual testing, upload, or deployment.
- Current runtime smoke is desktop `http://127.0.0.1:9899/app/` and installed
  WebUI `http://127.0.0.1:9909/app/` running simultaneously.
- Current validation baseline is
  `python -m unittest tests.test_ecorex_web_parallel_backend` with `79` tests
  plus desktop typecheck/build/runtime staging, WebUI/Linux/public packaging,
  and the release validator against
  `desktop/release-local-0023/win-unpacked`.
- Current public ZIP SHA256 is
  `E63D41F17D701B39F9947DAE9089FA0BF9A632D60CC29A39E1CB9B3C36BA4804`.
- Current Windows WebUI ZIP SHA256 is
  `2168D6F826221DBCD94BDC8F1F8CBC9C4E642A039C846E88E7C444866E9A19F2`.
- Current macOS WebUI tarball SHA256 is
  `CF3B4099B9B7425BA5A8EC976988BF0010E5A8BB75636B03B0AA90B81138AC24`.
- The 0023 rebuild includes the filesystem fallback, Feishu auth-convergence,
  managed built-in skill refresh, image-generation endpoint split,
  Admin/Models image auto hint, download-page, and validator changes.
- The default filesystem profile must not include `web_file_serve_root` or the
  user Home directory as a generic workspace root. Default no-profile access is
  workspace/cwd scoped; Home preview or broader roots require explicit config
  plus permission profile approval.
- `/api/file` must default to workspace/upload preview roots only. Do not use
  `web_file_serve_root="~"` as an implicit public preview root.
- Memory index sync, `MemoryService`, and `memory_get` must all call
  `authorize_file_access("read", ...)` before reading memory or knowledge
  files. A custom deny rule must hide denied memory files from lists and block
  direct content reads.
- Feishu split-flow auth is a hard convergence boundary. When `feishu_cli`
  returns `authRequired=true` or `available=false`, the next model turn must be
  text-only and ask the user to finish authorization/setup. Do not let the
  agent continue probing Feishu through `bash`.
- OpenAI image generation defaults to `gpt-image-2-pro` with fallback to
  `gpt-image-2` only for model/access unavailability. Use the official Images
  API parameters: `model`, `prompt`, `n`, `size`, `quality`, `output_format`,
  `output_compression`, `background`, and `moderation`. Do not send
  `response_format` for GPT Image models.
- OpenAI image routing is endpoint-sensitive. Text-only/no-input-image
  creation must use `/images/generations`; any edit, reference-image, local
  input image, or `image_url` request must use `/images/edits` with multipart
  `image` / `image[]`. Do not hard-code all image requests to `generations`,
  and do not accept a release that only tests text-only generation.
- Admin/Models image capability must surface and auto-suggest
  `gpt-image-2-pro` for OpenAI so UI hints match the runtime default.
- Do not claim `gpt-image-2-pro` connectivity unless
  `OPENAI_API_KEY` is set and
  `python scripts\check-openai-image-model.py --model gpt-image-2-pro --fallback-model gpt-image-2`
  succeeds. A missing-key result is not a model-connectivity pass.
- The final public download grid must show only Windows desktop, macOS desktop
  DMG choices, Windows WebUI, and macOS WebUI. The dual Win/Mac WebUI package
  and Linux/web service package may remain generated for internal compatibility
  but must be `archived`/hidden, not public `ready`, unless the release plan is
  explicitly changed.
- Download page image assets are release artifacts. The public zip must contain
  non-empty `site/assets/icon.png`, `site/assets/ecorex-app-preview.png`, and
  `site/assets/ecorex-ecosystem-hub.png`. The page should have image fallbacks
  so a bad static path does not show broken-image icons, but the real PNG files
  are still required by the validator.

## 2026-06-16 v0.1.12 Release Gate Addendum

- After any Agent Core, WebChannel, permission broker, skill, or packaging
  source change, all already-built desktop/WebUI/Linux/public artifacts are
  stale. Rebuild desktop runtime, Electron unpacked package, WebUI packages,
  Linux/web package, and public release ZIP before hand-test or upload.
- Do not validate only `desktop/runtime/ecorex-runtime`. Validate the packaged
  runtime inside `desktop/release-local-00xx/win-unpacked/resources/ecorex-runtime`
  and the installed WebUI runtime under `%LOCALAPPDATA%\EcoreX WebUI\runtime`.
  This catches cases where staging is correct but Electron packaging used old
  resources.
- After regenerating WebUI/Linux artifacts, update every ready artifact's
  `size` and `sha256` in `deploy/ecorex-site/manifest.json` before running
  `scripts/prepare-ecorex-public-release.ps1`. The public release script must
  fail when manifest hashes are stale.
- If Electron Builder repeatedly fails while downloading Electron or helper
  binaries from GitHub/mirrors, do not keep retrying blindly. When
  `desktop/node_modules/electron/dist` already contains the expected Electron
  runtime, build the local hand-test package with
  `--config.electronDist=node_modules/electron/dist` and record that fact in
  the release log. This keeps the package reproducible from the installed npm
  dependency and avoids wasting a release pass on transient network resets.
- Do not run `prepare-ecorex-public-release.ps1` and
  `validate-ecorex-release-artifacts.py` in parallel against the same public
  ZIP. The validator can hit a Windows file lock while the ZIP is still being
  written. Run public release generation first, then run validator.
- Required packaged runtime smoke for v0.1.12:
  - two `stream_response()` subscribers for the same `request_id` both receive
    the same `id: 0` terminal `done` event;
  - simple raw Feishu shell routes including `lark-cli`, `npx lark-cli`,
    `npx @larksuite/cli...`, and `node .../cli-main/scripts/run.js` are
    autorouted to `feishu_cli`;
  - complex Feishu shell forms with pipes, redirects, command separators, or
    multiple commands are not autorouted and must hard-stop with guidance.
- Filesystem boundary changes must be validated inside the packaged runtime,
  not only source. The validator must check `authorize_file_access`,
  filesystem profile evaluation, `read`/`ls` read hooks, `write`/`edit` write
  hooks, `send` read hooks, automatic memory write hooks, knowledge read hooks,
  and `/api/file` profile enforcement sentinels. Runtime smoke should include
  a custom profile where workspace files are allowed, deny-glob secrets such as
  `**/*.env` are blocked, and outside-workspace paths fail.
- WebUI permission state must follow the running runtime's configured
  `appdata_dir` when `ECOREX_USER_DATA` / `ECOREX_DESKTOP_USER_DATA` is not
  set. A freshly extracted WebUI smoke must show `/api/tool-permissions.auditPath`
  under that package/install state directory, not the global
  `%LOCALAPPDATA%\EcoreX\permissions` fallback. This prevents old installs or
  parallel WebUI instances from leaking stale permission mode into a new
  hand-test.
- Network/model-upload tools must fail closed when the permission broker is
  unavailable. The release validator must retain sentinels for `web_fetch`,
  `web_search`, and `vision` so direct tool invocation cannot bypass the
  shared boundary if AgentStream is not in the call path.
- Any renderer call routed through `window.ecorexDesktop.apiJson` must also be
  present in `desktop/electron/apiBridge.ts` allowlist. Add/update bridge
  allowlist entries in the same change as frontend API usage, then run
  `npm run typecheck` and package smoke. The concrete regression fixed in
  0018 was `GET /api/active-requests`.
- Codex-boundary wording is a release claim. v0.1.12 may say
  "Codex-boundary-inspired hardening" for policy/routing/current-process
  liveness, but it must not claim full Codex host parity until durable turn and
  process APIs, replayable event logs after runtime restart, product-level
  sub-agents, patch/worktree transactions, and full sandbox profiles exist.

本文记录 EcoreX 桌面端、WebUI、本地一键包、下载页和管理员后台的发布准则。每次改动发布前必须按本文检查，避免重复踩坑。

## 发布前固定流程

1. 同步最新源码和版本号，确认 `desktop/package.json`、`deploy/ecorex-site/manifest.json`、管理员 API 和脚本默认版本一致。
2. 构建桌面渲染端，再把 `desktop/dist` 同步到 WebUI 静态目录；WebUI 不能回退到旧 `channel/web/chat.html` 页面。
3. 运行 runtime staging，把 `channel/web/web_channel.py` 等运行时代码同步进 `desktop/runtime/ecorex-runtime`。
4. 生成 Linux Web 包、Windows/macOS 本地 WebUI 包、public release 包。
5. 回填下载页 `manifest.json` 中每个 ready artifact 的真实 `size` 和 `sha256`。
6. 部署 public release 后运行服务器检查脚本，并实际打开本地 WebUI 做一次 smoke test。
7. 最后再提交代码和推送 GitHub；不要只上传构建产物而不提交脚本和文档。

## WebUI 必查项

- `/app/` 是新桌面同款 WebUI 入口，`/chat` 和 `/` 必须兼容返回同一套新 Web App，不能再读旧 `chat.html`。旧入口仍会被用户书签、快捷方式和浏览器历史命中。
- 每次改 `desktop/src/**` 后必须重新执行桌面渲染端构建，并把 `desktop/dist` 同步到 `channel/web/static/app`。否则用户安装 WebUI 后仍会打开旧页面。
- WebUI 包验证必须启动“本次解压出来的包内 runtime”，不要只打开已经安装过的 `%LOCALAPPDATA%\EcoreX WebUI` 或旧快捷方式。旧安装进程可能继续占用端口并服务旧哈希 JS，导致误判最新包没有生效。
- WebUI 浏览器桥接层必须在 `POST /message` 前检查模型配置：本地没有可用模型时应提示用户登录企业账号或配置模型，不能继续让 runtime 用空 API Key 请求 OpenAI。
- WebUI 必须通过本地 `/client/*` 代理连接管理员后台，默认目标为 `https://www.ecoreai.cn/ecorex-agent/client`。不要让浏览器直接跨域请求后台，否则会遇到 CORS 和部署域名差异问题。
- 本地 fallback 登录只能用于无后台代理的离线本地场景。接入管理员后台后，登录失败必须显示失败，不能静默退回 `ecorex@ecorex.local` 或 `local@ecorex.local` 占位账号。
- 退出登录必须同时清理企业 session 和本地 fallback session；否则用户退出再登录新账号后，设置页可能仍显示旧账号。
- 刷新后若没有有效用户 token，页面应回到企业登录表单；`/client/model-config` 返回 `401 missing user token` 是正确未登录状态。
- 会话列表摘要不要展示 `N 条` 这类消息数量；它会让卡住的旧 pending 会话看起来仍在运行。旧缓存里的 pending 消息必须有过期归一化逻辑。
- 新安装默认必须是暗色模式。`desktop/index.html`、Electron 初始窗口背景、WebUI 注入的桌面桥和构建后的 `channel/web/static/app/index.html` 都要一起检查，不能只改 React state。
- 暗色模式正文主字体必须保持接近白色的高对比度；不要把主要消息文字降成 muted 灰。
- 聊天框下方必须保留本机访问权限选择，但 UI 形态必须是 Codex 同款的单个向上展开菜单；权限图标、当前权限和 token/上下文用量必须在同一水平 footer 内，不要回退成三枚横排按钮。
- 聊天输入区上方不要再加横向分隔线。`composer-zone` 只保留背景和内边距，WebUI 与桌面端都不能恢复 `border-top`。
- 切换会话、刷新页面或短暂断开 SSE 时，运行中任务必须继续跑；前端要保留 `request_id` 并用同一个 `/stream?request_id=...` 重连。只有用户点停止，或同一会话发送新消息进行插队时，才允许调用 `/cancel` 中断旧任务。
- 如果整个本地运行时已经退出导致旧 `request_id` 失效，再次进入时必须把旧 pending 气泡归一成“已暂停，输入新消息后继续”，不能残留“思考中”或直接暴露 `invalid request_id`。
- 上下文窗口估算必须包含工具输入/输出、推理/阶段内容、文件和图片引用；不要只对用户/助手纯文本长度做粗略估算。
- WebUI 与桌面端必须共用消息渲染能力：Markdown 富文本、工具折叠、文件链接、图片预览都要一致。Markdown 内的本地 `file://`、Windows 盘符路径和 macOS 绝对路径必须转换到 `/api/file?path=...`，不能让浏览器直接打开本地文件路径。
- WebUI 同一会话运行中再次发送时，应按桌面端行为先中断当前 request，再等待 session lock 释放并提交新消息；不要直接把 `session_busy` 暴露给用户。
- WebUI 和桌面端聊天气泡都必须保留一键复制文本入口，复制按钮用图标和 tooltip，不要只依赖用户手动选中文本。
- 桌面端和 WebUI 同机同时运行时必须使用不同端口、同一默认 workspace。桌面端默认 workspace 不应回退到 `~/cow`；EcoreX 默认统一为 `~/EcoreX`，否则安装登记、会话锁和文件查看会出现端间不一致。
- 运行时安装登记 surface 必须区分 `desktop` 和 `webui`；不要让桌面端 sidecar 把自己登记成 `webui` 覆盖本地 WebUI 的记录。
- 暗色/亮色模式的滚动条必须跟随主题变量；不要保留浏览器默认浅色滚动条或上下箭头。

## 浏览器控制和 CDP

- CDP 是 EcoreX 的第一优先级浏览器控制方式，默认端点为 `http://127.0.0.1:9222`，应优先连接或自动拉起 Chrome/Edge，再按配置回退到 Playwright 托管 Chromium。
- 安装版 WebUI 的 `config.json` 可能是极简配置。`config.py` 必须在加载配置后补齐 `tools.browser` 和 `chrome-devtools` MCP 默认值，不能只依赖 `config-template.json`。
- WebUI/桌面端的本地发布脚本生成 `config.json` 时也要写入相同 CDP 默认值，避免首次启动时行为和源码默认不一致。
- Python `playwright` 包是 CDP 客户端依赖，必须进入 core runtime requirements。`playwright install chromium` 只属于 Chromium fallback 能力包，不要在基础 WebUI 安装里强制下载浏览器内核。
- `chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222 --no-usage-statistics` 作为 MCP 补充能力注册；Windows 命令使用 `npx.cmd`，macOS/Linux 使用 `npx`。

## Runtime 任务卡死排查

- WebUI 出现“思考中”时先看 `%LOCALAPPDATA%\EcoreX WebUI\state\ecorex-webui.log`，区分前端缓存状态、后端真实执行中、工具超时未释放三类问题。
- 成功返回的工具调用也必须有收敛边界。不要只检测“同参数失败循环”；像 Feishu Base `has_more=true` 这种成功分页会让模型持续 offset/filter 查询，必须通过 skill 规则或工具脚本给出 `should_continue=false`。
- Codex 和 EcoreX 可能读取不同 skill 目录。影响产品行为的 skill 修复必须同步到 `C:\Users\user\EcoreX\skills\...`、`C:\Users\user\.codex\skills\...` 和打包内置 copy，并在正式包里验证。
- 小红书 Feishu 参考库读取必须先用 `--limit 30` 小页读取并执行 `scripts/select_feishu_references.py --page-count <n> --max-pages 3`。拿到 5-12 条强相关记录就停止；弱相关最多三页后报告缺口，不能无限翻页。
- Agent Core 不能只“提示模型不要用 raw bash 调飞书”。简单 `lark-cli ...`、`npx lark-cli ...`、`npx @larksuite/cli...` 和 `node .../cli-main/scripts/run.js ...` 必须在执行前自动转到 `feishu_cli`，继承权限、超时、取消和脱敏边界；带管道、重定向、`&&` 等复杂 shell 仍必须硬拦截并提示改用结构化工具。
- Windows 下 `subprocess.run(shell=True, timeout=...)` 可能只超时外层 `cmd.exe`，子 PowerShell 仍继续运行并持有 stdout/stderr pipe。Shell 工具必须用可杀进程组启动，并在超时时 `taskkill /PID <pid> /T /F`。
- 打包前必须验证一个超时 shell 命令能在接近设定超时时间返回，并且没有残留子 `powershell.exe` 继续扫描磁盘或占用 CPU。
- 工具流事件必须携带并优先使用 `tool_call_id`。如果只按 `bash`/`browser` 名称匹配，多次同名调用会把结束事件写到错误卡片，导致某个工具行永久 running。
- SSE 断开不等于任务终止。后端 `stream_response` 必须保留未完成请求的队列和 `request_to_session` 映射，前端缓存必须带 `requestId` 以便重连；只有 terminal event、显式 cancel、或运行时彻底退出后，才把本地 pending 收敛到 `done/cancelled/error/paused`。
- 同一个 `request_id` 可能被桌面端、WebUI、刷新后的页面同时订阅。SSE 不能用单个共享 `Queue.get()` 作为真实消费源；必须使用 replay/broadcast 事件日志和每个订阅者独立游标，否则一个页面会消费掉另一个页面的工具/文本/done 事件。
- 权限等待不是普通卡顿。`tool_permission_request` 必须能被 UI 看见，`full-access` 必须能让本机 shell/browser 工具直接通过，`read-only` 必须仍然挡住危险执行。

## 管理员后台与 Client Key

- `ECOREX_CLIENT_EVENT_KEYS` 必须包含当前所有客户端 key，例如：
  - `ecorex-desktop-v0.1.10`
  - `ecorex-desktop-v0.1.11`
  - `ecorex-desktop-v0.1.12`
  - `ecorex-web-v0.1.11-web.1`
  - `ecorex-web-v0.1.12-web.1`
- client key 是公开客户端标识，不是模型/API 密钥。它只允许访问登录和 client policy 通道；模型配置仍必须要求有效用户 token。
- 线上服务可能不是 `ecorex-admin-api.service`，也可能由 Docker Compose 管理。发布时必须确认真实运行进程的环境变量，而不是只修改未被容器使用的 env 文件。
- 当前生产 Docker Compose 使用 `/srv/ecorex-agent-admin/ecorex-admin-api.env`；脚本维护的 `/srv/ecorex-agent-admin/env/ecorex-admin-api.env` 不一定是活动文件。部署脚本需要合并两处 client keys。
- 验证顺序：
  - 无 client key 请求 `/client/model-config` 应返回 `403 invalid client key`。
  - 只有有效 client key、没有用户 token 时应返回 `401 missing user token`。
  - 有效 client key + 有效用户 token 时才返回模型配置。

## Windows/macOS WebUI 合并包

- 对用户只展示一个双端 WebUI 包：`EcoreX_<version>-webui-win-mac.zip`。
- 合并包根目录必须包含：
  - `Install EcoreX WebUI.cmd`
  - `Install EcoreX WebUI.command`
  - `windows/**`
  - `macos/**`
- Windows 入口调用 `windows/scripts/install-ecorex-webui-win.ps1`。
- macOS 入口调用 `macos/scripts/install-ecorex-webui-mac.sh`。
- macOS `.command` 和 `.sh` 在 ZIP 内必须保留 `0755` 可执行权限；普通 `Compress-Archive` 不可靠，应使用带 `external_attr` 的 zip helper。
- 单端 Windows/macOS 包可继续作为隐藏兼容下载项发布，但下载页卡片应只展示双端合并包，避免用户困惑。

## 下载页和 Manifest

- `manifest.json` 是下载页、管理员发布页和服务器校验的单一事实来源。任何 artifact 重新生成后必须同步更新 `size` 和 `sha256`。
- 下载页 JS 修改后必须更新 `index.html` 的 `site.js?v=...` cache buster。
- 如果旧单端 WebUI 包仍保留在线上，设置 `"visible": false`，但仍保持 `status: "ready"`，避免旧链接失效。
- Windows EXE 签名后 SHA256 会变化；如果 blockmap 不是同一次签名后生成的，不要声明 auto-update blockmap 有效。

## 编码和文件权限

- PowerShell 直接打印 UTF-8 中文时可能显示乱码，这不等于文件坏了。用 Node/Python 按 UTF-8 读取验证中文内容。
- 写入脚本、JSON、HTML、JS 时使用 UTF-8 无 BOM。
- macOS/Linux 脚本必须保持 LF。打 tar 包前运行 `bash -n` 校验安装脚本。
- Windows 本地测试产生的临时目录、PowerShell cache、安装目录日志不能提交。

## 发布校验命令

本地基础校验：

```powershell
python -m py_compile channel\web\web_channel.py desktop\runtime\ecorex-runtime\channel\web\web_channel.py deploy\ecorex-admin-api\ecorex_admin_api.py
node --check deploy\ecorex-site\site.js
```

Web Linux 包校验：

```powershell
bash -lc "cd /mnt/c/CowAgent && CHECK_INSTALLED=0 CHECK_HTTP=0 bash scripts/check-ecorex-web-release.sh release-artifacts/EcoreX_0.1.12-web-linux-service.tar.gz"
```

macOS 安装脚本校验：

```powershell
bash -lc "tar -xOzf /mnt/c/CowAgent/release-artifacts/EcoreX_0.1.12-webui-macos-universal.tar.gz ecorex-webui-macos-universal-0.1.12/scripts/install-ecorex-webui-mac.sh | bash -n"
```

双端 ZIP 结构必须检查：

```powershell
@'
import zipfile
z = zipfile.ZipFile("release-artifacts/EcoreX_0.1.12-webui-win-mac.zip")
for name in [
    "ecorex-webui-win-mac-0.1.12/Install EcoreX WebUI.cmd",
    "ecorex-webui-win-mac-0.1.12/Install EcoreX WebUI.command",
    "ecorex-webui-win-mac-0.1.12/windows/runtime/channel/web/web_channel.py",
    "ecorex-webui-win-mac-0.1.12/macos/runtime/channel/web/web_channel.py",
]:
    assert name in z.namelist(), name
for name in [
    "ecorex-webui-win-mac-0.1.12/Install EcoreX WebUI.command",
    "ecorex-webui-win-mac-0.1.12/macos/scripts/install-ecorex-webui-mac.sh",
]:
    assert ((z.getinfo(name).external_attr >> 16) & 0o777) == 0o755, name
'@ | python -
```

服务器发布后校验：

```bash
sudo -n CHECK_CADDY=0 bash /srv/ecorex-agent-admin/server/check-ecorex-server-release.sh
```

管理员后台连接校验：

```powershell
Invoke-WebRequest -UseBasicParsing `
  -Uri http://127.0.0.1:<port>/client/model-config `
  -Headers @{ "X-EcoreX-Client-Key"="ecorex-web-v0.1.12-web.1"; "X-EcoreX-Device-Id"="smoke-test" }
```

预期未登录结果是 `401 missing user token`，不是 `403 invalid client key`。

## 手测清单

- 打开 `http://127.0.0.1:<port>/app/`，显示桌面同款 WebUI。
- 打开 `http://127.0.0.1:<port>/chat`，也显示同一套新 WebUI。
- 首次打开默认暗色模式，滚动条、标题栏、正文文字都匹配暗色主题。
- 聊天框下方的单个权限菜单可切换“完全访问”，刷新 `/api/tool-permissions` 后 mode 仍是 `full-access`。
- 运行中输入新消息并按 Enter，旧气泡变为已暂停/已中止，新消息开始执行；不能出现“该会话正在执行中，请稍后再试”。
- 切换会话、刷新页面或重新打开 WebUI 页面后，仍在运行的任务能用原 `request_id` 继续接收结果；如果运行时已退出导致请求失效，旧运行气泡显示已暂停而不是继续生成中。
- 消息气泡右上角复制按钮可复制富文本对应的纯文本内容。
- 退出登录后刷新页面，出现企业登录表单。
- 使用管理员后台创建的用户登录后，顶部和设置页显示真实邮箱。
- 发送消息前，如果后台没有模型配置，显示清晰的“没有可用模型配置”提示；如果后台有模型配置，应自动同步后发送。
- 下载页只展示双端 WebUI 合并包，不展示隐藏的单端兼容包。

## JSON Encoding Gate

- Files that are parsed by Windows PowerShell release scripts must be valid
  when read by PowerShell's default `Get-Content` behavior and by UTF-8-aware
  Python/Node readers.
- Avoid raw non-ASCII text in JSON templates that release scripts may pipe into
  `ConvertFrom-Json`. Prefer ASCII text or escaped JSON strings. Windows
  PowerShell 5 can decode UTF-8 without BOM as ANSI and corrupt Chinese text
  before JSON parsing.
- Before packaging, run both checks for JSON templates used by installers or
  runtime staging:
  - `Get-Content config-template.json | ConvertFrom-Json`
  - `python -c "import json,pathlib; json.loads(pathlib.Path('config-template.json').read_text(encoding='utf-8'))"`
- `config-template.json` must keep `tools.feishu_cli`, CDP browser defaults,
  and chrome-devtools MCP `--browserUrl` defaults after this check. Do not fix
  encoding by dropping required runtime defaults.
- `EcoreX_<version>-public-release.zip/checksums.json` must be UTF-8 without
  BOM. Strict Python `json.loads(bytes.decode("utf-8"))` must pass for both
  `site/manifest.json` and `checksums.json`; do not rely only on
  `utf-8-sig` readers.
- After regenerating release artifacts, run:
  `python scripts/validate-ecorex-release-artifacts.py --version <version>`.
  This is the local gate for manifest/download size and SHA matching, BOM-free
  JSON, stale WebUI asset rejection, and required host-boundary runtime files.

## Windows Signing Gate

- Run `npm run sign:win:preflight` from `desktop/` before any long Windows
  signed packaging run. The `package:win:signed` script also runs this first.
- A valid certificate record with `HasPrivateKey=True` is not enough. The
  Certum/SimplySign private-key provider must expose a key container to
  `signtool`.
- If preflight reports `Smart Card service: Stopped` or `SimplySign CSP key
  containers: none visible`, do not rebuild/upload a Windows setup. First
  unlock/login to Certum SimplySign/proCertum SmartSign and, if needed, start
  services from an elevated shell:
  - `Start-Service SCardSvr`
  - `Start-Service CertPropSvc`
- Do not reuse an older signed `EcoreX_0.1.12_x64-setup.exe` after runtime
  source changes. Mark `windows-x64` as `pending-signature` until the current
  runtime is signed and the NSIS setup is regenerated.
- Do not upload the whole `release-artifacts` directory. Publish only artifacts
  listed as `ready` or `ready-unsigned` in `deploy/ecorex-site/manifest.json`,
  or the validated `EcoreX_<version>-public-release.zip`. Quarantine stale
  signed installers under `release-artifacts/stale-do-not-publish/`.
- After preflight passes, rebuild in this order:
  - `npm run package:win:signed`
  - copy the signed setup into `release-artifacts`
  - update manifest size/SHA256/AuthentiCode state
  - regenerate `EcoreX_0.1.12-public-release.zip`

## Agent Host Boundary Gate

- `host_diagnostics` and `feishu_cli` must be present in `/api/tools` for both
  desktop and WebUI runtimes.
- `host_diagnostics(action=status)` must return sanitized runtime status. It
  may report CDP as not ready when Chrome is closed, but it must show the
  configured endpoint and a clear error instead of silently falling back.
- `feishu_cli(action=status)` must report whether `lark-cli` is available and
  authenticated. Windows packages should bundle `runtime/tools/bin/lark-cli.exe`
  when `ECOREX_LARK_CLI_EXE` or `C:\cli-main\bin\lark-cli.exe` is available.
  macOS packages should bundle `runtime/tools/bin/lark-cli` when
  `ECOREX_LARK_CLI_DARWIN` is available; otherwise the installer must log the
  npm fallback without blocking WebUI startup.
- chrome-devtools MCP must use
  `chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222 --no-usage-statistics`.
  Do not ship a config that uses `--autoConnect` as the default path.
- Default chrome-devtools MCP noninteractive startup must be allowed only for
  the built-in command signature. A workspace `mcp.json` entry named
  `chrome-devtools` with any other command must not bypass permission prompts,
  and `read-only` must block even the default startup.
- MCP stdio, SSE, and streamable-http startup must obey the permission mode.
  `read-only` blocks MCP startup and MCP tool calls; `full-access` allows them
  and writes audit records.
- MCP `tools/call` JSON-RPC `error` and MCP `isError=true` results must be
  surfaced as `ToolResult.error`, never as successful text beginning with
  `Error:`. A release build that treats MCP failure as success can make the
  agent loop on a broken tool chain.
- MCP `tools/list` JSON-RPC errors must mark the server failed. Do not ship a
  build that reports an MCP server as ready with zero tools after a list error.
- MCP tool calls from `chrome-devtools` must map to the browser permission
  category. Other MCP tools must map to the `mcp` external capability category.
- Permission broker failures for `bash`, `browser`, `feishu_cli`, and MCP must
  fail closed. Do not ship a build where a broken permission check silently
  allows dangerous local execution.
- `bash` and `feishu_cli` subprocess execution must honor the active request
  cancel event and kill the child process tree on cancel or timeout. Stop/new
  message/session switching must not leave a hidden child process running while
  the UI shows "thinking".
- Bash timeouts must be normalized before process launch; string/null/negative
  model arguments must not start a process and then crash deadline handling.
- MCP stdio tool calls and BrowserService queued operations must also honor the
  active request cancel event. Stop/new message must return quickly rather than
  waiting for the full browser/MCP timeout.
- BrowserService must not start a second worker while a cancelled/timed-out
  Playwright worker is still shutting down.
- MCP stdio shutdown must kill the process tree, and streamable-http SSE
  response reading must have a total deadline; keepalive comments cannot keep a
  tool call alive indefinitely.
- `read-only` must block local `write`/`edit` file mutations and skill
  add/delete/enable/disable in addition to shell/browser/Feishu/MCP execution.
- `read-only` must also block `env_config set/delete`, `send`,
  `scheduler create/delete/enable/disable`, `evolution_undo`, `web_fetch`,
  `web_search`, and `vision`. These are host capabilities even when they do
  not look like shell execution: they can mutate local config, expose local
  files, create background work, restore files, access the internet, download
  to disk, or upload local image bytes to model APIs.
- Scheduler background execution must use noninteractive `scheduler`
  authorization before running a due task. Scheduled `tool_call` must authorize
  the concrete target tool as well; scheduler must never be a bypass around
  `bash`, browser, MCP, Feishu, network fetch/search, vision, or file mutation
  permissions.
- `host_diagnostics` may collect sanitized read-only status, but any diagnostic
  sub-probe that starts an external helper must obey that helper's
  noninteractive permission boundary. In particular, Feishu status must not
  launch `lark-cli auth status` when `feishu_cli` is denied by read-only or
  smart-ask without remembered grants.
- SkillService add/open/close/delete must go through `skill_write`
  authorization. In `smart-ask` or `always-ask` without an interactive
  permission decision, skill mutation must fail closed.
- `SkillService` package installation must reject invalid skill names, `..`,
  absolute paths, and zip-slip writes. All skill add/delete operations must
  stay under the workspace `skills` directory.
- `SkillService` must also reject Windows reserved device names, trailing-dot
  names, silent overwrite of existing custom skills, and silent built-in skill
  overlays unless explicit replace/override flags are present.
- Startup builtin-skill sync must never overwrite an existing workspace
  same-name skill. Workspace skill overlays are the durable self-repair path for
  built-in skill bugs.
- Background self-evolution/skill repair is a noninteractive host capability.
  It must not start unless noninteractive permission allows the file-write,
  skill-write, and helper-shell capabilities it may use. `smart-ask` without
  remembered grants and `read-only` must skip the background pass instead of
  waiting for a permission prompt that no UI can answer.
- Logs and diagnostics must mask nested `token`, `password`, `authorization`,
  API key, and secret values. Do not print raw environment override values.
- WebUI may run passwordless only on loopback (`127.0.0.1`, `localhost`, or
  `::1`) for one-click local installs. A non-loopback bind such as `0.0.0.0`,
  `::`, or a LAN address must fail startup unless `web_password` is configured.
  Message, config, model, history, file, and permission APIs are host-control
  surfaces and must not be public without auth.
- A stale stream `invalid request_id` must not leave the UI thinking forever.
  The renderer should load session history and replace the pending bubble when
  a final assistant message is already persisted; only fall back to paused when
  no final history exists.
- Web request finalization is a release blocker. Worker completion, worker
  exception, and pre-worker `produce(context)` exception must all unregister
  the cancel token, preserve the SSE queue until the terminal event is
  consumed, and emit a terminal `done`/error event so the UI cannot keep
  thinking until idle timeout.
- MCP tools must be model-visible only through namespaced names such as
  `mcp__server__tool`; they must never overwrite first-party tools like
  `bash`, `browser`, `feishu_cli`, or `host_diagnostics`. The wrapper must
  preserve the remote MCP tool name for RPC execution. Chrome DevTools MCP
  tools must share the browser/CDP convergence budget.
- Tools that cache runtime config fields must implement `apply_config()` and
  ToolManager/AgentInitializer must call it after every config merge. Do not
  ship a build where `tool.config` changes but cached fields such as
  `cwd`, `package`, or `auto_install` still use constructor defaults.
- `feishu_cli action=ensure` must honor `auto_install=false` and explicit
  `install_if_missing=false`. A diagnostic or ensure path must not silently run
  `npm install -g` after the admin/runtime config disabled auto-install.
- Skill load failures must be visible to the agent. Malformed `SKILL.md` files,
  missing descriptions, unreadable skill directories, and missing requirements
  must be surfaced in the skills prompt or `host_diagnostics`, not only debug
  logs.
- Renderer tail events after SSE `done` are release-critical. If the backend
  keeps a post-done tail window for `voice_attach` or other attachments, the
  frontend must not clear request state in a way that drops those events.
- Runtime media URLs such as `/uploads/...` are HTTP assets, not local
  filesystem paths. Message rendering must not route them through local file
  preview APIs.
- Same-request SSE must be broadcast/replay based. A new EventSource connection
  must not supersede or drain an older one; desktop and WebUI may observe the
  same running request at the same time.

## 2026-06-16 Release Pitfalls

- Message rendering is shared by Desktop and WebUI. Before releasing, verify
  clickable behavior for:
  - Markdown links such as `[OpenAI](https://openai.com)`.
  - Bare `http://` and `https://` URLs.
  - `file://` URLs.
  - Windows paths such as `C:\Users\user\Desktop\foo.txt`.
  - UNC paths and macOS/Linux absolute paths such as `/Users/name/file.txt`.
- Desktop local paths must call the Electron `openPath` bridge and therefore
  stay inside the existing permission boundary. WebUI local paths must open
  through `/api/file?path=...`; browsers must not navigate directly to raw
  local filesystem paths.
- Electron external links must allow `http:`, `https:`, and `mailto:` only.
  Keep `file:`, `javascript:`, custom protocol, and OS shell protocol opens out
  of `shell.openExternal`.
- After every renderer change, run `npm run build`, then sync
  `desktop/dist` to `channel/web/static/app` before WebUI/Linux packaging.
  Update `scripts/validate-ecorex-release-artifacts.py` so the current hashed
  JS asset is required and the previous hashed JS asset is forbidden.
- For the 2026-06-16 production build the required renderer asset is
  `index-B_LYG2V7.js`; `index-CjBkNLMl.js` is stale.
- Certum SimplySign can expose the certificate in `Cert:\CurrentUser\My` while
  non-elevated `signtool` still fails `After Private Key filter, 0 certs were
  left`. If that happens, start `SCardSvr` and `CertPropSvc`, unlock
  SimplySign/proCertum, and run the signing step from an elevated PowerShell.
  Do not mark Windows ready until `Get-AuthenticodeSignature` returns `Valid`
  on the final NSIS setup.
- Production WebUI CDP depends on `npx` because default MCP uses
  `npx chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222
  --no-usage-statistics`. On Ubuntu production hosts, install a modern Node.js
  runtime before final verification. The 2026-06-16 host uses Node.js
  `v22.22.3` and npm/npx `10.9.8`; after restart, `chrome-devtools` MCP loaded
  29 tools.
- macOS desktop DMGs cannot be produced on the Windows release workstation.
  Use `.github/workflows/ecorex-desktop-release.yml` on a `macos-15` runner,
  then copy the real `EcoreX_0.1.12_arm64.dmg` and
  `EcoreX_0.1.12_x64.dmg` into the public release manifest with real
  size/SHA. Do not relabel old v0.1.11 DMGs as v0.1.12.
- Xiaohongshu/Feishu skills must use `feishu_cli` first. Simple raw
  `bash lark-cli`, `npx @larksuite/cli`, and `node .../scripts/run.js`
  calls must autoroute to `feishu_cli`; complex raw shell
  loops, `field-list --format json`, unbounded `has_more=true` pagination, and
  large raw page dumps are release blockers.
- After any runtime, packaging script, or host-boundary change, regenerate the
  desktop installer, WebUI win/mac zip, single-platform compatibility packages,
  Linux service tarball, public release zip, and manifest hashes. Older package
  hashes are not valid after these changes.
- If only the local Win/Mac WebUI packages are regenerated during a hand-test
  pass, explicitly mark the Linux service tarball and public release zip as
  stale until they are regenerated from the same source tree. Do not deploy a
  mixed set of artifacts from different rebuild passes.
- After any renderer or WebUI static asset change, run desktop runtime staging
  again before Electron packaging:
  - `cd desktop`
  - `npm run build`
  - `npm run stage:runtime:win`
  - `electron-builder --win ...`
  The desktop app can otherwise contain a fresh `app.asar` while the packaged
  sidecar runtime still serves old files from
  `resources/ecorex-runtime/channel/web/static/app`, causing `/app/` or
  `/chat` to show an old WebUI even though the desktop renderer is new.
- Runtime staging and Linux/WebUI packaging must clear
  `channel/web/static/app` before copying a new `desktop/dist`. Copying over the
  directory is not enough; old hashed JS files can remain side-by-side and make
  a package serve stale UI. Update
  `scripts/validate-ecorex-release-artifacts.py` whenever the renderer hash
  changes so the previous JS hash is treated as stale.
- Linux/Web service packaging must copy the contents of `desktop/dist` into
  `runtime/channel/web/static/app`, not the `dist` directory itself. A package
  containing `runtime/channel/web/static/app/dist/index.html` instead of
  `runtime/channel/web/static/app/index.html` is invalid.
- The release validator must parse the actual packaged `index.html` and confirm
  that the referenced JS/CSS assets are the expected current hashes. Checking
  that a new JS file merely exists somewhere in the archive is not enough.
- The public release zip must contain exactly the ready artifacts listed in
  `deploy/ecorex-site/manifest.json`. Extra files under `site/downloads/` are
  stale release risk and must fail validation.
- The release validator must be run against the unpacked desktop build as well
  as the public artifacts:
  `python scripts/validate-ecorex-release-artifacts.py --version <version> --desktop-dir desktop\release-local-XXXX\win-unpacked`.
  This is a release blocker because the desktop product has two host surfaces:
  Electron `app.asar` and `resources/ecorex-runtime`. The validator must prove
  that the packaged broker still classifies dangerous tools globally, that
  noninteractive runtimes fail closed when no approval UI exists, that optional
  capability install/preinstall routes through Electron permission checks, and
  that main-process external URL opening is restricted to the allowed schemes.
  Passing only the WebUI/Linux/public zip checks is not enough after any
  Agent Core, Electron host, or packaging change.

## Runtime Status Gate

- Runtime status must be backend-authoritative. The renderer may cache pending
  bubbles for fast restore, but release behavior must use `/api/active-requests`
  to decide whether a request is truly still running.
- `/api/active-requests` must exist in WebUI and desktop sidecar runtimes and
  return active request metadata without message content:
  `request_id`, `session_id`, `cancelled`, `state`, `created_at`,
  `age_seconds`, and `stream_available`.
- `CancelTokenRegistry.snapshot()` must be present in the packaged runtime.
  This is the source of truth for in-process active requests and must mark a
  cancelled-but-not-yet-unregistered request as `state="cancelling"`.
- The renderer must consume `stream_available`. When it is `false`, it should
  refresh persisted session history and wait/retry instead of opening a normal
  `/stream?request_id=...` EventSource that the backend already says is gone.
- After repeated SSE reconnect failures, the renderer must re-check
  `/api/active-requests` before marking a message paused. A backend-active
  request stays pending; a cancelled active request shows stopping; only a
  missing request with no final history becomes paused.
- The session list must synthesize backend-active sessions even when they are
  outside the latest paged session list and no local cached pending bubble
  remains.
- History recovery must accept non-text completions with media/tool steps or
  stored sequence metadata. Generated image/file-only replies must not be
  treated as unfinished merely because the assistant text body is empty.
- Session switching and page refresh must reconnect to the backend-reported
  `request_id` when the process is still alive. If the process is gone or the
  request id is invalid and no final history exists, the UI must normalize the
  old pending bubble to paused instead of leaving it as thinking.
- The release validator must inspect both packaged runtime source and renderer
  bundles for `/api/active-requests`, `activeRequests`, and cancel-registry
  snapshot support.
- After any renderer hash changes, update the validator's current JS/CSS asset
  expectations and rebuild all packages. A package that serves an old renderer
  can silently lose active-request recovery even when backend code is correct.

## Managed Built-In Skill Gate

- Packaged built-in skills can be masked by old workspace copies under
  `~/EcoreX/skills`. Release validation must check that official managed
  built-ins such as `image-generation` and `create-xiaohongshu-note` refresh
  stale workspace copies unless the copy contains `.ecorex-custom-override`.
- A same-name built-in skill override is only considered intentional when it was
  installed through the explicit override path and contains
  `.ecorex-custom-override`. Plain old copied built-ins are release-managed and
  must be updated from the packaged `skills/` directory.
- Image-generation release checks must verify the runtime copy actually loaded
  by WebUI/Desktop, not only the repository `skills/` directory. A local smoke
  should confirm `~/EcoreX/skills/image-generation/scripts/generate.py`
  contains `DEFAULT_MODEL = "gpt-image-2-pro"` after startup refresh.
- OpenAI image requests must not be hardcoded to one endpoint. Text-only or
  no-input-image creation uses `/images/generations`; image edits,
  reference-image requests, local input images, or `image_url` use
  `/images/edits` with multipart `image` / `image[]`. The OpenAI branch must
  not send legacy `response_format`.

## GitHub Actions macOS DMG Gate

- macOS desktop DMGs must be built on a GitHub-hosted macOS runner or a real
  macOS host. Do not relabel older DMGs as a new version when the Windows host
  cannot build them locally.
- `actions/upload-artifact` can fail even after old artifacts are deleted
  because GitHub recalculates artifact storage usage every 6-12 hours. When a
  same-day release is blocked by artifact quota, use the workflow-dispatched
  GitHub Release upload path for DMG and SHA256 assets instead of waiting for
  artifact storage to recalculate.
- The workflow-dispatched release upload must use `contents: write`,
  `gh release create` if the target tag does not exist, and
  `gh release upload --clobber` for both `EcoreX_*_<arch>.dmg` and
  `EcoreX_<arch>.sha256`.

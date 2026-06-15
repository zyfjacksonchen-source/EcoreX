# EcoreX 打包发布准则

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
- `chrome-devtools-mcp@latest --autoConnect` 作为 MCP 补充能力注册；Windows 命令使用 `npx.cmd`，macOS/Linux 使用 `npx`。

## Runtime 任务卡死排查

- WebUI 出现“思考中”时先看 `%LOCALAPPDATA%\EcoreX WebUI\state\ecorex-webui.log`，区分前端缓存状态、后端真实执行中、工具超时未释放三类问题。
- Windows 下 `subprocess.run(shell=True, timeout=...)` 可能只超时外层 `cmd.exe`，子 PowerShell 仍继续运行并持有 stdout/stderr pipe。Shell 工具必须用可杀进程组启动，并在超时时 `taskkill /PID <pid> /T /F`。
- 打包前必须验证一个超时 shell 命令能在接近设定超时时间返回，并且没有残留子 `powershell.exe` 继续扫描磁盘或占用 CPU。
- 工具流事件必须携带并优先使用 `tool_call_id`。如果只按 `bash`/`browser` 名称匹配，多次同名调用会把结束事件写到错误卡片，导致某个工具行永久 running。
- SSE 断开不等于任务终止。后端 `stream_response` 必须保留未完成请求的队列和 `request_to_session` 映射，前端缓存必须带 `requestId` 以便重连；只有 terminal event、显式 cancel、或运行时彻底退出后，才把本地 pending 收敛到 `done/cancelled/error/paused`。
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
  -Uri http://127.0.0.1:9924/client/model-config `
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

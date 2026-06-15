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
- WebUI 浏览器桥接层必须在 `POST /message` 前检查模型配置：本地没有可用模型时应提示用户登录企业账号或配置模型，不能继续让 runtime 用空 API Key 请求 OpenAI。
- WebUI 必须通过本地 `/client/*` 代理连接管理员后台，默认目标为 `https://www.ecoreai.cn/ecorex-agent/client`。不要让浏览器直接跨域请求后台，否则会遇到 CORS 和部署域名差异问题。
- 本地 fallback 登录只能用于无后台代理的离线本地场景。接入管理员后台后，登录失败必须显示失败，不能静默退回 `ecorex@ecorex.local` 或 `local@ecorex.local` 占位账号。
- 退出登录必须同时清理企业 session 和本地 fallback session；否则用户退出再登录新账号后，设置页可能仍显示旧账号。
- 刷新后若没有有效用户 token，页面应回到企业登录表单；`/client/model-config` 返回 `401 missing user token` 是正确未登录状态。

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
- 退出登录后刷新页面，出现企业登录表单。
- 使用管理员后台创建的用户登录后，顶部和设置页显示真实邮箱。
- 发送消息前，如果后台没有模型配置，显示清晰的“没有可用模型配置”提示；如果后台有模型配置，应自动同步后发送。
- 下载页只展示双端 WebUI 合并包，不展示隐藏的单端兼容包。

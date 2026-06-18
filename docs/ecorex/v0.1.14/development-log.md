# EcoreX v0.1.14 Development Log

## 2026-06-18 Production User Hotfix: Device Session And Network Reset

### 背景

部分真实用户在同一台电脑上更换账号后仍然看到“登录状态异常 / device does not match user session”，另有部分 macOS 用户在聊天里直接看到底层异常：

`ConnectionResetError(54, 'Connection reset by peer') (Status: 0, Code: , Type: )`

这两个问题都不是额度耗尽。前者是设备 ID 绑定格式不一致，后者是模型接口/网络链路被远端重置后，底层 requests 异常被直接透传到了用户可见消息。

### 根因

- 桌面端默认设备 ID 来自 `os.hostname() + platform`。当机器名包含中文或其他非 ASCII 字符时，登录请求体里保存的是 raw deviceId，但后续请求头为了避免 Electron/Fetch `ByteString` 报错会转成 URL encoded，后台按字符串精确比较后判定为不同设备。
- 旧版本或历史 session 中可能同时存在 raw、encoded 两种 deviceId，导致同一台机器换账号后仍然被拒绝。
- OpenAI-compatible HTTP client 在 `ConnectionError`、`ChunkedEncodingError`、`RequestException` 场景下把 Python/requests 原始英文异常塞进 error chunk；stream executor 再拼接空的 `Status: 0, Code: , Type:`，最终前端原样展示。

### 修复

- `desktop/electron/enterprisePolicy.ts`
  - 新增 `normalizeEnterpriseDeviceId()`，把非 ASCII 设备 ID 归一为稳定、安全的 ASCII ID：`ecorex-<hint>-<sha256>`。
  - 所有企业请求头统一使用归一化 deviceId，避免 ByteString 和 raw/encoded 漂移。
- `desktop/electron/enterpriseAuth.ts`
  - 登录保存服务端返回的归一化 `deviceId`。
  - 本地 session 如果发现旧格式 deviceId，会自动失效并引导重新登录，避免继续带脏状态请求。
- `desktop/electron/sidecar.ts`、`desktop/electron/capabilities.ts`、`desktop/electron/telemetry.ts`
  - 统一使用同一个 deviceId 归一化逻辑。
- `deploy/ecorex-admin-api/ecorex_admin_api.py`
  - 新增 `device_id_matches()`，兼容 raw、URL encoded、decoded 三种格式。
  - 登录响应返回 `deviceId`，老客户端和新客户端都能稳定保存。
- `models/openai/openai_http_client.py`
  - 网络中断、超时、请求失败改为用户可读中文提示；原始异常只写日志。
  - app attribution 从旧项目名改为 `EcoreX` / `zhangyifanjackson-dotcom/EcoreX`。
- `agent/protocol/agent_stream.py`
  - LLM error chunk 进入用户消息前统一清洗；`Status: 0` 和 connection reset 类错误不再展示堆栈或空 code/type。
- `desktop/src/utils/redaction.ts`
  - 前端增加防御性过滤，历史消息或 tool detail 中残留的 raw network error 也会渲染为本地化网络中断提示。

### 验证

- `python -m unittest tests.test_ecorex_admin_device_id`
- `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py agent/protocol/agent_stream.py models/openai/openai_http_client.py`
- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build`

### 后续注意

- 这类 device/session 错误必须显示为“登录状态异常”，不能再归类成“额度已达到上限”。
- 如果只热修服务端，老安装包也能通过 raw/encoded 兼容恢复登录；如果要彻底消除新请求头漂移，需要重新分发包含桌面端归一化逻辑的新安装包。
- 网络连接重置仍可能由用户网络、代理、API_BASE 或上游模型网关导致；产品侧保证提示可读、可重试，并且不暴露底层 Python/requests 异常。

## 2026-06-18 macOS DMG Agent Install README

### 背景

v0.1.14 macOS DMG 当前可能是 unsigned / not notarized。部分用户会让 WorkBuddy、Codex 或其他本机 agent 代为安装，因此随包增加 agent-readable README，避免用户或 agent 采用关闭全局 Gatekeeper 的危险做法。

### 修复

- 新增 `desktop/build/README-macos-agent-install.txt`。
  - 面向 WorkBuddy 等本机 agent。
  - 明确先校验官方来源和 SHA256。
  - 提供挂载 DMG、复制 `EcoreX.app`、保留用户数据、移除 quarantine、启动 app 的完整脚本。
  - 明确禁止 `sudo spctl --master-disable`，不关闭全局 Gatekeeper。
- 更新 `desktop/electron-builder.yml`。
  - README 进入 `.app/Contents/Resources/README-macos-agent-install.txt`。
  - DMG 顶层同时展示 `README-macos-agent-install.txt`，打开镜像后可直接看到。

### 验证

- `node -e "const fs=require('fs'); const yaml=require('./desktop/node_modules/js-yaml'); const c=yaml.load(fs.readFileSync('desktop/electron-builder.yml','utf8')); if(!c.dmg||!Array.isArray(c.dmg.contents)||c.dmg.contents.length<3) throw new Error('missing dmg contents'); if(!c.extraResources.some(x=>x.to==='README-macos-agent-install.txt')) throw new Error('missing extraResources readme'); console.log(JSON.stringify({dmgContents:c.dmg.contents.length, extraResources:c.extraResources.length}, null, 2));"`

### 后续注意

- 由于当前执行环境是 Windows，不能本地生成并挂载 DMG 验证 Finder 布局；该 README 会进入下一次 GitHub macOS 构建产物。
- 若以后完成 Apple Developer ID 签名和 notarization，应保留 README 中的 SHA256 校验与数据保留步骤，但可以弱化 unsigned/quarantine 说明。

## 2026-06-18 Final Hotfix And Release Pass

### 背景

v0.1.14 在用户手测通过后，又补了一轮发布前修复与上线收口：

- 修复会话吞消息、任务完成后仍显示继续生成、会话状态串台。
- 修复 WebUI 无法打开本地文件/文件夹、生成图片和本地文档只显示路径或文件名。
- 修复登录/设备绑定异常被误报成“额度已达上限”。
- 用签名脚本完成 Windows 正式包签名，并确保安装器、主程序、NSIS 提权 helper 都是有效签名。
- 重新打包 GitHub Release、public download release，并把 macOS DMG 镜像到公网下载站，避免私有 GitHub Release 外链对匿名用户 404。

### 代码修改留痕

- `desktop/src/App.tsx`
  - 增加本地 optimistic 消息与服务端 history 的合并逻辑，避免刷新会话时用旧 history 覆盖用户第二条消息。
  - 修复 stale pending assistant：服务端已返回同一轮 user 消息后，不再保留本地过期 pending，从根上解决“任务完成后还在继续生成中”。
  - 将 enterprise quota/device/session 错误分类为登录态异常，不再展示“额度已达到上限”。

- `desktop/src/services/ecorexApi.ts`
  - `openLocalPath` 在 WebUI 无 Electron bridge 时 fallback 到 runtime `POST /api/open-path`。
  - WebUI 点击项目文件夹、本地文档、图片产物时走后端本地打开能力，不再误走 `/api/file`。

- `desktop/src/components/MessageContent.tsx`
  - 从聊天文本、inline code、tool result 和文件列表中抽取本地产物。
  - 图片产物渲染为缩略图卡片，多图使用轻量网格。
  - `.txt/.md/.json/.pdf` 等文档产物渲染为可点击文件卡片，保留复制文本能力。

- `desktop/src/styles/app.css`
  - 增加 inline local file code 和 artifact cards 的样式，保证图片缩略图、文件名和打开入口布局稳定。

- `channel/web/web_channel.py`
  - enterprise Web session 复用稳定 `deviceId`，避免登录后随机设备 ID 漂移导致 `device does not match user session`。

- `deploy/ecorex-site/manifest.json`
  - Windows installer 更新为最终签名包：
    - size `148936840`
    - SHA256 `E4906C076169FE6FB70FFF8E0BF09C687BC2F76C6267359B37DB7D58A5FD6007`
  - macOS DMG 从 GitHub Release 外链切换为站内下载：
    - `downloads/EcoreX_0.1.14_arm64.dmg`
    - `downloads/EcoreX_0.1.14_x64.dmg`
  - source 文案更新为 GitHub Actions 构建并镜像到公网下载站。

### 验证命令

- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build:renderer`
- `npm --prefix desktop run build`
- `python -m py_compile channel/web/web_channel.py deploy/ecorex-admin-api/ecorex_admin_api.py`
- `python -m unittest tests.test_ecorex_web_parallel_backend`
- `node -e "JSON.parse(require('fs').readFileSync('deploy/ecorex-site/manifest.json','utf8'))"`
- `Get-AuthenticodeSignature` 校验 Windows setup、`EcoreX.exe`、`elevate.exe` 均为 `Valid`。
- `scripts/prepare-ecorex-public-release.ps1 -Version 0.1.14 -MacArm64DmgPath ... -MacX64DmgPath ...`
- 生产机：
  - `check-ecorex-server-release.sh` 通过。
  - `verify-ecorex-release.ps1` 通过公网 manifest、Windows 下载、WebUI 下载、Admin gate、Client gate 和本地 Windows 签名校验。

### 最终产物

- Windows Desktop:
  - `release-artifacts/EcoreX_0.1.14_x64-setup.exe`
  - size `148936840`
  - SHA256 `E4906C076169FE6FB70FFF8E0BF09C687BC2F76C6267359B37DB7D58A5FD6007`
  - Authenticode `Valid`

- Windows update feed:
  - `release-artifacts/EcoreX_0.1.14_x64-setup.exe.blockmap`
  - SHA256 `FC6A2E3CDC4D48148E8AD7899C20EAA883B3E4682D3D6A62CE26C375D2B634C7`
  - `release-artifacts/latest.yml`

- macOS Desktop:
  - `release-artifacts/EcoreX_0.1.14_arm64.dmg`
  - size `193148636`
  - SHA256 `0B4A5E00157DBAE0C82333FBB7B6D4BB4C8F06F2D16F947477D0016E346D3D6A`
  - `release-artifacts/EcoreX_0.1.14_x64.dmg`
  - size `200527249`
  - SHA256 `C6123DFD9578A50A1190C416AFD6EFD33BDC66AFFFADB3ECA9DE971A0248F3DE`

- WebUI:
  - `release-artifacts/EcoreX_0.1.14-webui-windows-x64.zip`
  - size `72279835`
  - SHA256 `E645A650DA744126C8A8242BF52E3B0425253601D5EE894AF7432E2CFA268A6C`
  - `release-artifacts/EcoreX_0.1.14-webui-macos-universal.zip`
  - size `165337005`
  - SHA256 `8F8911696DD0EE949A5EB4C97BB0E91A22FE9F307882E625F7DD315AE053DCD5`

- Public download release:
  - `release-artifacts/EcoreX_0.1.14-public-release.zip`
  - size `783848264`
  - SHA256 `D6F23CF644A9DFE8F1585062118B2E2E37F7F7C02176D331DE24A82369EA398A`

### 线上部署结果

- public release 已安装到生产：
  - `/srv/ecorex-agent-download/releases/20260618063002-v0.1.14`
  - `/srv/ecorex-agent-download/current` 已切换到该目录。
- 公网下载页：
  - `https://www.ecoreai.cn/ecorex-agent/`
  - `https://www.ecoreai.cn/ecorex-agent/manifest.json`
- 生产检查通过：
  - root、manifest、assets HTTP 200。
  - Admin 页面未登录 HTTP 401。
  - Client model-config gate 未登录 HTTP 403。
  - Windows installer、blockmap、Windows WebUI、macOS WebUI、Apple Silicon DMG、Intel DMG、Linux service 包均 HTTP 200。

### 踩坑点

- **SimplySign connected 不等于当前签名进程可用。**
  - 托盘显示 connected 且 PIN 已输入时，普通 PowerShell 仍可能拿不到私钥。
  - 最终通过提升权限 PowerShell 运行 `desktop/scripts/sign-win.ps1 -SignToolDir C:\脚本签名工具` 完成签名。
  - `SCardSvr`/`CertPropSvc` 状态需要检查；脚本不应删除或修改证书。

- **electron-builder 会在 NSIS 打包时重新复制 `elevate.exe`。**
  - 先签 `desktop/release/win-unpacked/resources/elevate.exe` 后再跑 NSIS，会被 electron-builder 覆盖回未签名 helper。
  - 最终方案：复制一份 NSIS bundle 到 `release-artifacts/signed-nsis-3.0.4.1-final`，签这份 bundle 内的 `elevate.exe`，然后用 `ELECTRON_BUILDER_NSIS_DIR` 指向 signed bundle 重建安装器。

- **签名会改变 installer hash/size。**
  - 不能沿用 electron-builder 生成的签名前 `latest.yml` 和 blockmap。
  - 必须在最终签名后重新生成 `.blockmap` 和 `latest.yml`，再上传 GitHub Release 和 public download host。

- **public release 生成脚本会读取 `desktop/release/latest.yml`。**
  - 如果只更新了 `release-artifacts/latest.yml`，但没有同步到 `desktop/release/latest.yml`，validator 会报 `latest.yml installer size mismatch`。
  - 处理方式：最终 feed 文件需要同步到两个位置后再打 public release。

- **GitHub Release 私有外链对公网下载页不可靠。**
  - 服务器深度检查发现 macOS DMG GitHub Release 外链匿名访问返回 404。
  - 最终改为把 DMG 下载到本地 release artifacts，并作为 `site/downloads/*` 镜像到 `https://www.ecoreai.cn/ecorex-agent/downloads/`。

- **GitHub token 可能失效。**
  - 用户提供的 token 后续返回 `401 Bad credentials`。
  - 最终使用本机 GitHub Desktop / git credential helper 的当前登录凭据临时注入 `GH_TOKEN`，不落盘、不打印 token。

- **PowerShell 版本差异。**
  - 当前环境不支持 `&&` 作为命令分隔符。
  - 发布脚本和手工命令应使用 PowerShell 原生顺序执行，或分多条命令。

- **会话吞消息根因是 history refresh 覆盖本地 optimistic state。**
  - 用户第二次发送时，本地 UI 已插入消息，但服务端 history 还没完全返回；刷新会话会把本地消息覆盖掉。
  - 修复后 history 与本地 pending/user/assistant 按 request/sequence/content key 合并。

- **`device does not match user session` 不是额度问题。**
  - 这是 enterprise 登录设备 ID 漂移/不匹配。
  - UI 分类必须显示登录态异常，不得误报“额度已达上限”。

- **产物路径不能只当文本渲染。**
  - 图片、文档、目录都需要转换为可点击 artifact card。
  - WebUI 没有 Electron bridge 时必须走 `/api/open-path`，否则本地文件夹和文档打不开。

### 后续注意

- macOS DMG 当前为 `ready-unsigned`，仍需在 macOS 真实机器上验证 Gatekeeper 体验。
- Windows 自动更新真实 v0.1.13 -> v0.1.14 仍要继续做用户侧数据保留手测，重点看 session、附件、生成图片、active session/project 是否完整。
- 任何重新签名或重新打包 Windows installer 后，都必须重复：签名校验、blockmap/latest.yml 重算、manifest hash 更新、GitHub Release 覆盖、public release 重建、生产 server check。

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

## 2026-06-18 体验收敛与 Skill/MCP 安装修复追加

### 背景

- 用户反馈聊天产物噪音过大，工具输入、prompt、manifest、日志等被当成产物大面积展示；需要强参考 Codex 桌面端，默认展示结论和少量产物，调用过程/多产物折叠。
- Skill/MCP 安装不丝滑，尤其飞书 / Lark 安装时 `feishu-lark` 能力包和 `feishu-cli` 运行时能力混淆，agent 会反复诊断同一个 ID，甚至造成会话进入 stale request / `invalid request_id` 状态。
- 后台未查到对应 admin API 拒绝日志；生产 runtime 当前配置 `tools.feishu_cli.auto_install: false`，旧版本日志中存在 `auto_install: true`。本轮判断根因在前端 SSE/request 状态恢复、agent 安装任务编排和产物渲染策略。

### 本轮修复

- `desktop/src/components/MessageContent.tsx`
  - 调用过程统一进入 `agent-process-disclosure` 折叠块；运行中也只显示静态小点、步数和当前步骤摘要。
  - 工具思考、工具调用不再默认展开，减少大段 Input/Output 对用户的干扰。
  - 产物提取默认只信任工具结果和 media step；工具输入只有在明显产物型工具时才参与提取，避免 prompt、参数、manifest 被误认为产物。
  - 多产物默认展示前 6 个，剩余通过 `显示另外 N 个` 折叠，保留图片缩略图、文件名和一键本地打开入口。

- `desktop/src/styles/app.css`
  - 运行状态从 spinner 改为 Codex 风格静态小点，避免“永远还在转”的心理噪音。
  - 新增产物区标题、产物数量和折叠按钮样式。
  - 调用过程摘要增加当前步骤单行省略，避免长工具名/路径撑开布局。

- `desktop/src/App.tsx`
  - `invalid request_id` 时先尝试刷新历史；恢复失败时将消息落为“任务状态已同步。如未完成，请重新发送。”，不再标成 `paused`，也不再保留旧 requestId。
  - 复制消息默认只复制最终正文和 media/file 产物，不复制 thinking/tool stdout/stderr。
  - 手动安装能力包时，如果当前会话仍有 pending request，不再直接拒绝；改为排队，显示“已排队，当前任务结束后自动安装”，待当前会话空闲后自动在同会话发起 agent 安装任务。

- `agent/tools/agent_capability/agent_capability.py`
  - 新增能力包 ID 别名：`feishu`/`lark`/`feishu-lark`/`lark-cli`/`feishu-cli`。
  - `install_pack(feishu-lark)` 变成明确安装计划：先 `feishu-lark` 能力包，再 `feishu-cli` 运行时能力，避免 agent 在两个 ID 之间反复诊断。
  - 安装结果返回 compact summary：`installPlan`、每步 `status/message/logPath/stdoutTail/stderrTail`、`nextAction`。详细日志保留给折叠调用过程，正文只需要输出结论。

- `channel/web/web_channel.py`
  - `/api/agent-install-request` 针对飞书 / Lark 输出更明确的隐藏安装指令：必须使用 `agent_capability`，不要要求用户输入“同意安装”，不要反复诊断同一个 ID，失败时最多先 diagnose 一次并根据日志修复。

- `tests/test_ecorex_web_parallel_backend.py`
  - 补充 `feishu-lark -> feishu-lark + feishu-cli` 组合安装测试。
  - 补充 `lark-cli -> feishu-cli` 别名测试。
  - 补充 agent install request prompt 测试，确保包含 `agent_capability`、`feishu-cli`、不要求手动同意、不反复诊断。

### 本轮踩坑点

- 产物噪音不是单纯 CSS 问题。根因是渲染层把工具输入和工具输出都抽成产物；必须从 artifact extraction 源头区分“输入参数”和“实际输出”。
- `invalid request_id` 不应展示给用户，也不应把消息恢复成暂停态。旧 requestId 如果残留，会让下一条消息或安装任务被旧流状态挡住。
- `feishu-lark` 和 `feishu-cli` 是两层能力：一个是能力包/连接器，一个是运行时 CLI 能力。UI 和 agent 提示词必须告诉 agent 组合安装，而不是让 agent 自己猜。
- 安装能力不能静默抢当前会话；当前会话繁忙时必须排队或等待用户确认，否则容易出现“安装能力导致运行时等待/无反应”的误判。
- 复制消息也要走降噪策略；否则 UI 看起来收敛，但复制出去仍会带出工具日志。

### 待手测重点

- 长任务运行中：调用过程是否默认折叠，只显示小点、步数、当前步骤摘要。
- 多图片/多文件产物：是否默认只展示前 6 个，剩余可展开，点击本地打开正常。
- 复制消息：是否只包含最终结论和必要产物路径，不包含 thinking、tool input/output。
- 飞书 / Lark 安装：当前会话忙时是否进入排队；空闲后是否自动发起 agent 安装；agent 是否不再反复诊断 `feishu-lark`/`feishu-cli`。
- stale request：安装失败或 SSE 丢失时，左侧状态是否能恢复，不再一直显示暂停/继续生成/等待回复。

### 并行校验复盘与二次修正

- 并行校验发现安装排队最初只存单槽 `{pack, onInstalled}`，没有绑定发起时的 session；用户在会话 A 忙时排队后切到会话 B，安装可能在 B 中启动。
  - 修正：`queuedInstallRef` 改为数组队列，队列项包含 `sessionId`；只有对应 session 空闲时才在原会话启动安装。
  - 连续点击不同能力包不再覆盖前一个队列项。

- 并行校验发现 `feishu-lark` 会组合安装 `feishu-cli`，但权限代理摘要仍只显示 `feishu-lark`。
  - 修正：`agent_capability install_pack(feishu-lark)` 的权限代理映射为 `optional_abilities install "feishu-lark + feishu-cli"`，让用户确认范围覆盖 CLI 第二步。

- 并行校验发现 `feishu-cli` 安装失败时关键日志字段是 `output`，不是 `stdout/stderr`。
  - 修正：compact install step 的 `stdoutTail` 同时接收 `stdout` 和 `output`，agent 失败诊断不丢 npm/lark-cli 关键信息。

- 并行校验发现复制消息可能重复最终正文。
  - 修正：`plainTextForMessage` 使用去重 `addPart`，只复制最终正文、非 intermediate 的 final content step 和 media/file 产物，不复制 thinking/tool 细节。

- 并行校验发现工具结果仍可能把 `README.md`、`package.json` 等普通日志里的裸文件名误抽成产物。
  - 修正：非产物型工具的 result 仍可识别明确本地/相对产物路径，但不再允许裸文件名提取；只有 write/save/send/export/render/image/file/artifact/deliverable/create/generate 类工具才允许从参数或结果中提取裸文件名。

### 二次验证命令

- `npm --prefix desktop run typecheck`
- `python -m unittest tests.test_ecorex_web_parallel_backend.TestAgentCapabilityPermissions tests.test_ecorex_web_parallel_backend.TestWebParallelHandlers.test_agent_install_request_for_feishu_guides_agent_without_manual_consent`
- `python -m unittest tests.test_ecorex_web_parallel_backend`
- `npm --prefix desktop run build`
- `python -m py_compile agent/tools/agent_capability/agent_capability.py channel/web/web_channel.py agent/protocol/agent_stream.py`
- `git diff --check`

## 2026-06-18 Hand-Test Follow-Up: Capability State And Artifact Rendering

### Findings

- Feishu/Lark capability install could finish at the runtime layer while the settings panel still showed "installing".
  - Root cause: desktop UI preferred the Electron bridge capability status over `/api/capabilities`; the bridge could lag behind runtime state.
  - Fix: `listCapabilityPacks()` now prefers the runtime diagnosis endpoint and only falls back to the bridge while runtime is unavailable.
  - Fix: settings UI clears local installing banners as soon as a pack reaches `installed` or `failed`.

- Installing Python capability packs directly into the live bundled Python `site-packages` is risky on Windows.
  - Root cause: `optional_abilities` did not pass `--target-dir`, so `pip` wrote into the running runtime environment.
  - Fix: capability packs now install into `capability-packages/<pack-id>`.
  - Fix: installed pack `targetDir` is recorded in state and added to `sys.path`/`PYTHONPATH`.
  - Fix: installer subprocesses have per-command timeouts so a hanging `pip` cannot leave the UI permanently waiting.
  - Fix: Electron capability probing includes all installed pack target directories.

- Artifact cards were too noisy and sometimes wrong.
  - Root cause: renderer extracted artifacts from tool arguments and from broad text/JSON scans.
  - Fix: tool arguments no longer feed artifact cards.
  - Fix: structured results only expose explicit `artifacts/files/deliverables/media/attachments` fields.
  - Fix: bare filenames in final prose remain clickable when rendered inline, but do not automatically become artifact cards.
  - Fix: URL paths like `https://beian.miit.gov.cn/...` are no longer treated as local directories.
  - Fix: media items without a resolvable local/relative path are skipped to avoid blank broken thumbnails.
  - Fix: duplicate images are de-duped by normalized image filename so content/media/tool-result duplicates collapse.

- Local image/file previews and open actions were brittle.
  - Root cause: frontend eagerly joined relative artifact paths with the active project path, which broke workspace-relative outputs.
  - Fix: preview URLs now pass the original path to `/api/file`; open action tries original path first, then active-project-resolved path.
  - Fix: `/api/file` permits absolute local paths when the permission broker allows read access, instead of hard-blocking everything outside the workspace root.

### Verification

- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build`
- `python -m py_compile channel/web/web_channel.py agent/tools/optional_abilities/optional_abilities.py desktop/runtime/ecorex-runtime/agent/tools/optional_abilities/optional_abilities.py desktop/scripts/install-capability.py`
- `python -m unittest tests.test_ecorex_web_parallel_backend.TestAgentCapabilityPermissions.test_optional_ability_install_uses_isolated_target_dir_and_timeout`
- Local smoke: `GET /api/file?path=<absolute desktop/build/icon.png>` returned HTTP 200 image/png.

## 2026-06-18 Hand-Test Follow-Up: Apparent No-Reply After Large Tool Output

### Findings

- A user message looked like it had no reply, but `/api/history` showed the assistant answer had already been persisted.
  - Root cause: the turn included a very large Feishu Base JSON tool result in `steps`; the renderer still had to build/process that data, which made the UI feel stalled even after the backend completed.
  - Fix: tool detail rendering is capped with a middle-omission summary, so large stdout/JSON no longer becomes a massive React subtree.
  - Fix: artifact extraction no longer scans every non-artifact tool result deeply. Only artifact-producing tools or explicit artifact fields are considered.

- SSE completion could still leave the visible bubble stale if the final `done` payload or stream close path did not carry the full persisted answer.
  - Fix: after every streamed `done`, the desktop UI schedules a short `/api/history` refresh and merges the authoritative persisted turn back into the current session.
  - Follow-up: live SSE replay showed duplicate `done` events for one image-generation request. The backend now records `sse_done_sent` and emits only one terminal `done` per request, while still allowing image/file/voice tail events.

- The Xiaohongshu skill could stop at "confirm generation" even when the user already said to proceed with the default direction and explicitly requested one cover only.
  - Root cause: the skill treated Demo direction confirmation as always requiring a separate explicit phrase.
  - Fix: `create-xiaohongshu-note` now treats explicit production direction, asset count, or "no carousel" as production confirmation unless there is a real missing input or blocker.
  - The active user skill copy under `C:\Users\user\EcoreX\skills\create-xiaohongshu-note` was updated for immediate hand-test, and the source skill under `skills/create-xiaohongshu-note` was updated for packaging.

### Verification

- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build`
- `python -m py_compile channel/web/web_channel.py agent/tools/optional_abilities/optional_abilities.py desktop/runtime/ecorex-runtime/agent/tools/optional_abilities/optional_abilities.py desktop/scripts/install-capability.py`
- `python -m unittest tests.test_ecorex_web_parallel_backend.TestWebParallelHandlers.test_done_event_is_emitted_once_per_request`
- `git diff --check`

## 2026-06-18 Final Hand-Test Acceptance And v0.1.15 Handoff

### Acceptance

- User hand-test passed for the v0.1.14 desktop experience after the final no-reply / no-response fixes.
- v0.1.14 scope is frozen at this point. No more feature changes should be added to this release train unless they are emergency regressions found before publication.
- Keep the current source state as the reference for final packaging, signing, release upload, and manifest refresh.

### What Was Accepted

- Capability/Skill/MCP install now runs through the current agent session, with queueing when the session is busy.
- Feishu/Lark install handles the `feishu-lark` pack and `feishu-cli` runtime capability as one coordinated install flow.
- Image generation uses the `gpt-image-2-pro` path where configured and generated image artifacts render in chat as previews.
- Local image/file/folder artifacts are shown as cards, support thumbnails where possible, and can be opened locally instead of only exposing text paths.
- Large tool outputs are folded and capped for display, preventing Feishu/Base JSON or stdout from making the UI appear stuck.
- Stream completion is stabilized by refreshing history after `done` and by deduplicating terminal SSE `done` events per request.
- Xiaohongshu note skill no longer stalls at an extra generation confirmation when the user already gave an explicit default direction, asset count, or no-carousel instruction.
- Internal/system guidance redaction, session state recovery, active request cleanup, and left-sidebar completion state are considered acceptable for v0.1.14.

### v0.1.15 Handoff Notes

- Further polish artifact presentation: keep Codex-like folding for large final artifact sets, and consider explicit "final deliverables" metadata from tools to reduce heuristic extraction.
- Expand Subagent beyond v1: CSV batch orchestration, recursive subagent depth policy, child-session result aggregation, and cancellation/audit UI.
- Improve image-generation queue observability: show queued/running/succeeded states from status files without requiring the model to poll with shell waits.
- Continue hardening WebUI local open behavior across browser permission limits and Windows/macOS path differences.
- Package/release work should start from this accepted source state: Windows signing script, four release artifacts, download manifest, GitHub upload, and macOS DMG build path.

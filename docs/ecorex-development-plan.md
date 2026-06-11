# EcoreX 开发执行文档与开发边界

本文档定义从 CowAgent 到 EcoreX 桌面端 AI Agent 的执行路径。后续开发会转为 goal 长任务推进，因此本文档也是防跑偏边界：每个阶段必须持续更新开发留痕和验收清单。

## 1. 总体架构

```text
EcoreX Desktop
├─ Electron Main
│  ├─ 窗口、托盘、菜单、自动更新
│  ├─ Python sidecar 生命周期管理
│  ├─ 本地安全存储和企业策略缓存
│  └─ 原生文件选择、深链、诊断包导出
├─ React Renderer
│  ├─ Codex 式双列 Chat Workbench
│  ├─ File Preview Drawer
│  ├─ Settings Center
│  └─ 明暗模式与 token 系统
├─ CowAgent Python Sidecar
│  ├─ agent core
│  ├─ tools / skills / MCP
│  └─ web/channel API
└─ EcoreX Admin Web
   ├─ 用户创建和管理
   ├─ 用量监控
   ├─ 错误日志回溯
   ├─ 策略和权限
   └─ 设备、审计、版本灰度
```

## 2. 技术栈选择

桌面端：

- Electron：保证 Windows/macOS 一致体验。
- React + TypeScript：桌面 UI。
- Vite：前端构建。
- shadcn/ui：基础组件源码归项目所有。
- Origin UI：组合组件参考，吸收模式后本地化。
- Tailwind CSS：只使用 token，不硬编码颜色。
- TanStack Query：会话、配置、Skill/MCP、文件状态。
- Zustand 或 Jotai：面板、主题、当前会话、预览状态。

管理端 Web：

- React + TypeScript。
- shadcn/ui + TanStack Table。
- 独立路由和权限体系。
- 后端可用 Node/NestJS/Fastify 或现有企业后端承接。

后端：

- 第一阶段不把 CowAgent 后端改写为 TypeScript。
- Python 继续负责 agent core、tools、skills、MCP、channel、文件和联网能力。
- TypeScript 负责 Electron、React、Admin Web、策略控制面。

## 3. 品牌与兼容策略

必须区分外显品牌和内部兼容字段。

| 名称 | 用途 | 推荐值 |
| --- | --- | --- |
| `displayName` | UI、窗口标题、bot 昵称、聊天身份 | `EcoreX` |
| `productName` | 安装包、桌面应用、更新器 | `EcoreX` |
| `productSlug` | 新配置、新路径、新云端 API | `ecorex` |
| `compatSource` | 旧 Skill/MCP/channel 路由兼容 | `cowagent` |
| `legacyCli` | 原 CLI 兼容 | `cow` |
| `newCli` | 新 CLI 入口 | `ecorex` |

禁止：

- 禁止全局替换 `cowagent`。
- 禁止在 Skill Hub、MCP、channel source 未适配前改变内部路由 key。
- 禁止移除 `cow` CLI 和 `~/cow` 兼容。

推荐：

- 增加统一 `BrandingConfig`。
- 外显 EcoreX，内部保留 `compatSource=cowagent`。
- 增加 source alias：`ecorex -> cowagent`。
- Skill namespace 支持 `ecorex`、`cowagent`、`openclaw`。

## 4. UI 开发边界

必须实现：

- 橙色品牌 token。
- 明暗双模式：Light / Dark / System。
- Codex 桌面端式双列主界面：左 session/项目上下文侧栏，右 chat 主工作区。
- 禁止误做成两个等权业务列或常驻三栏技术控制台。
- 文件预览点击后出现，不做常驻右栏。
- 设置统一入口：模型、通道、Skill、MCP、权限、文件、诊断都收进去。
- 管理员能力不塞进桌面主界面，单独做 Admin Web。
- hover 展示明细，文案面向用户，不堆技术名词。
- 图标优先，陌生图标必须有 tooltip。
- 多圆角，所有圆角走 token。
- 动态组件只服务状态表达。

禁止：

- 禁止组件硬编码颜色、圆角、阴影、动画时长。
- 禁止把 Skill、MCP、Models、Channels 做成一排主导航。
- 禁止把调试面板常驻暴露给普通用户。
- 禁止用假指标填充管理台。

## 5. 管理员 Web 范围

Admin Web 独立交付，目标是企业管理员通过网页统一管理用户和设备，桌面用户安装后开箱即用。

必须包含：

- 用户创建、邀请、禁用、角色、部门。
- 设备绑定、设备状态、版本、最后在线时间。
- 用量监控：用户、部门、模型、任务、Skill/MCP 调用。
- 错误日志回溯：按用户、设备、版本、会话、错误码查询。
- 审计日志：文件、shell、联网、外发、Skill、MCP、管理员操作。
- 策略管理：文件权限、联网权限、shell、外发、human-in-the-loop、模型、Skill/MCP。
- 连接器模板：飞书等应用模板、bot 外显名称、兼容 route。
- 版本灰度：stable、beta、internal。

Admin Web 不直接执行用户本地文件操作，只下发策略和模板。

## 6. 权限与 human-in-the-loop

权限模式建议：

- Smart Ask：低风险自动执行，高风险询问。
- Always Ask：每次敏感动作询问。
- Allow in Workspace：工作区内允许，工作区外询问。
- Read Only：只读模式。
- Custom：用户自定义。

高风险动作：

- 删除、覆盖、移动文件。
- 执行 shell/cmd。
- 发送文件到外部通道。
- 安装未知来源 Skill。
- 启动未知 MCP server。
- 访问用户未授权目录。

human-in-the-loop 节点要求：

- 在 chat 中以明确卡片出现。
- 说明 EcoreX 要做什么、影响范围、可选项。
- 支持允许本次、始终允许同类、拒绝、查看明细。
- 不用技术堆砌吓用户，但必须保留安全边界。

## 7. 多 Agent 并发协调

允许并发多 Agent，但必须受控，不能跑偏。

推荐机制：

- `AgentCoordinator`：统一管理并发任务、队列、取消、超时。
- `GoalLedger`：记录当前 goal、边界、完成标准、已做决策。
- `TaskLease`：每个 Agent 获得明确任务租约，超过范围必须停止并回报。
- `SharedArtifactIndex`：记录所有产物、文件、日志、测试结果。
- `DriftGuard`：检测是否偏离当前 goal、是否修改了禁止范围。
- `HumanGate`：关键节点回到用户确认。

默认并发：

| 类型 | 默认并发 |
| --- | --- |
| 普通 chat | 1 到 2 |
| 独立研究/搜索 | 2 到 4 |
| 文件批处理 | 1 到 2 |
| 浏览器自动化 | 1 |
| MCP server 启动 | 按需 |
| 管理台后台任务 | 队列化 |

每个 goal 结束或中断前必须更新 `docs/ecorex-dev-log.md`。

## 8. 运行时体积与能力包策略

桌面端默认安装包只内置核心运行时，满足启动、聊天、基础文件授权、联网请求、Settings、诊断与能力安装器可用。以下重型能力必须做成首次使用安装或管理员预置能力包：

- 飞书 / Lark 连接器。
- Slack、Discord、Telegram、WeChat、DingTalk、企业微信等长尾 IM 通道。
- 语音输入、音频转写、TTS 相关依赖。
- Playwright / Chromium 浏览器自动化。
- Office/PDF 重型解析。
- numpy/pandas 等高级数据处理。
- 通义千问、智谱、Gemini 等需要厂商 SDK 的模型入口。

产品要求：

- 普通用户不需要主动知道该安装哪个包。EcoreX 必须在任务发起时根据用户意图、附件类型和工具需求自动识别缺失能力。
- 自动识别后进入 human-in-the-loop：说明需要安装什么、预计体积、影响范围，并提供“安装并继续 / 跳过继续 / 取消”。
- 安装失败必须有用户可读原因、日志入口、重试入口和管理员处理建议，不能只显示 Python traceback。
- 管理员可在打包或组织策略中预置能力包，避免企业网络下用户首次使用时下载失败。
- 能力包状态、安装日志、失败原因必须结构化存储，供桌面端 Settings 和 Admin Web 复用。
- 企业环境需要支持 PyPI 镜像、内网包源或离线能力包缓存；这是后续 Admin Web/运维阶段的必做项。

当前实现入口：

- 能力清单：`desktop/runtime-packs/capabilities.json`。
- 核心依赖：`desktop/runtime-packs/core-requirements.txt`。
- 跨平台安装器：`desktop/scripts/install-capability.py`。
- Windows 包装入口：`desktop/scripts/install-capability-win.ps1`。
- Electron IPC：`ecorex:list-capability-packs`、`ecorex:install-capability-pack`。
- Renderer 自动识别：发送前根据文本和附件触发能力安装确认。

macOS 特别约束：

- 已签名 `.app` 不能在首次使用时向 `Contents/Resources` 写入 pip 包，否则可能无权限或破坏代码签名。
- macOS 能力包必须安装到用户数据目录，再通过 sidecar `PYTHONPATH` 注入。
- Windows 当前仍可把能力包装进 per-user 安装目录的 runtime；后续若支持 Program Files per-machine 安装，也应迁移到用户 capability target。

## 9. 阶段计划

### Phase 0：基线盘点与测试护栏

交付：

- 当前能力清单。
- API/配置/路由清单。
- 不可改边界。
- smoke test：启动、聊天、文件上传、文件读、web_search、MCP 加载。

### Phase 1：Electron POC

交付：

- Electron 启动 Python sidecar。
- Windows/macOS 可运行。
- 旧 Web Console 或新 Demo 可加载。
- 日志和退出流程可控。

边界：

- 不改 agent core。
- 不改内部 source。

### Phase 2：品牌兼容层

交付：

- 外显名称、图标、窗口标题、聊天身份为 EcoreX。
- `compatSource=cowagent` 可用。
- `ecorex` CLI alias 初步可用，`cow` 保留。
- 工作区迁移策略明确。

### Phase 3：React/shadcn 工作台

交付：

- 橙色 token。
- 明暗模式。
- Codex 式双列主界面。
- Settings Center。
- File Preview Drawer。
- human-in-the-loop 卡片。

### Phase 4：功能对齐

交付：

- 会话、消息、停止生成。
- 文件上传、点击预览、发送。
- 联网搜索和网页抓取。
- Skill 安装、启用、发现、调用。
- MCP 添加、连接、发现、调用。
- 模型、通道、权限设置。

### Phase 5：Admin Web

交付：

- 用户创建和管理。
- 设备管理。
- 用量监控。
- 错误日志回溯。
- 策略下发。
- 审计日志。

当前实现状态：

- 已有独立 SQLite Admin API：`deploy/ecorex-admin-api/ecorex_admin_api.py`。
- 已部署到 `https://www.ecoreai.cn/ecorex-agent/admin/api/*`，与 Admin Web 共用 Basic Auth。
- 已完成用户创建、禁用/启用、角色更新、用量聚合、错误事件写入/标记已读、能力包策略保存。
- 尚未完成设备绑定、SSO、部门/模型维度、配额、桌面端自动遥测上报和策略自动下发到客户端。

### Phase 6：企业治理

交付：

- Skill/MCP 来源策略。
- 文件权限策略。
- 外发策略。
- shell 策略。
- 审计和诊断包。

### Phase 7：打包发布

交付：

- Windows 签名安装包。
- macOS notarization。
- 自动更新。
- 灰度发布。
- 回滚方案。
- Windows/macOS 开箱即用：普通用户安装后只需要登录或绑定组织，无需命令行、Python、Node、Git、手动端口或 CowAgent 路径配置。

## 10. 开发边界

强约束：

- 不重写 agent core。
- 不删除旧 Web Console，直到新 UI 通过真实用户验收。
- 不全局替换 `cowagent`。
- 不改变 Skill/MCP/channel 内部路由，除非同时实现 alias 和迁移。
- 不默认暴露高风险文件删除、shell、外部发送。
- 不在桌面端硬编码企业密钥。
- 不让多 Agent 自由发散修改无关模块。

可改范围：

- 新增 Electron 工程。
- 新增 React/shadcn 组件。
- 新增 token 和主题系统。
- 新增 API adapter。
- 新增 branding config。
- 新增 Admin Web。
- 新增企业策略、审计、诊断接口。

谨慎改范围：

- `config.py` 默认配置。
- CLI 入口。
- channel preset/source。
- Skill namespace 校验。
- MCP clientInfo。

默认不改范围：

- agent planning/execution core。
- tool execution core。
- Skill 安装核心逻辑。
- MCP transport 实现。
- 已有渠道消息收发协议。

## 11. 验收策略

代码层验收不足以通过。本项目采用两层验收：

- 工程验收：测试、类型检查、构建、打包、接口兼容。
- 真实用户验收：按 `docs/ecorex-acceptance-checklist.md` 模拟普通用户、管理员、IT 运维完整使用。

任何阶段进入下个阶段前，必须更新：

- `docs/ecorex-dev-log.md`
- `docs/ecorex-acceptance-checklist.md`
- 当前阶段已知风险
- 下一阶段边界

## 12. 当前遗漏点补充

这些点容易在后续长 goal 中被忽略，进入实现前需要补设计或补验收：

- 企业登录：SSO、企业邀请码、设备绑定、离职禁用、账号切换。
- 网络环境：企业代理、离线模式、内网限制、搜索 provider 不可用时的降级。
- 能力包源：PyPI 镜像、离线包缓存、管理员预置包版本锁定、安装失败重试策略。
- 数据治理：日志脱敏、文件路径脱敏、提示词和输出留存周期、用户删除数据。
- 安全防护：prompt injection、网页内容诱导外发、本地文件越权、MCP/Skill 供应链风险。
- 设备运维：诊断包导出、崩溃恢复、自动更新失败回滚、版本兼容矩阵。
- 工作区迁移：`~/cow` 到 `~/.ecorex` 的迁移、回滚和用户确认。
- 可访问性：键盘导航、焦点态、色彩对比、缩放、屏幕阅读器基础语义。
- 国际化：中文、英文，以及飞书/Lark 场景中名称和时区显示。
- 通知系统：后台任务完成、等待确认、失败重试、免打扰。
- 成本控制：模型用量预算、并发上限、异常循环熔断、管理员配额。
- 法务合规：隐私政策、用户授权记录、企业审计导出、日志保留策略。
- 支持流程：错误码、用户可读故障说明、运维可查调用链、客服交接材料。

## 13. Definition of Done

整体完成标准：

- 用户看到的产品名、bot 身份、图标、窗口标题均为 EcoreX。
- 明暗模式完整，橙色品牌一致。
- 主界面符合 Codex 桌面端式布局，设置收口，文件点击后预览。
- Windows 和 macOS 普通用户安装后开箱即用，不需要自行安装运行时依赖或手动启动后端。
- 重型能力不默认撑大安装包；首次使用时由 Agent 自动识别、请求授权并安装，管理员也能预置能力包。
- 能力包安装失败时有清晰反馈、日志入口和重试/管理员处理路径。
- 首次登录或组织绑定后自动拉取企业策略、模型、通道、Skill/MCP 模板和权限配置。
- 管理员 Web 可创建用户、看用量、查错误日志、下发策略。
- Skill 能安装、启用、发现、调用。
- MCP 能添加、连接、发现、调用。
- 会话无死循环，长任务可停止、可恢复、可追踪。
- human-in-the-loop 节点清晰。
- 权限确认不打扰日常使用，但高风险动作有安全边界。
- 外部 Skill/MCP 在 `compatSource=cowagent` 下继续可用。
- 多 Agent 并发受控，不越界修改。
- 长 goal 中断后能通过留痕恢复上下文。

## 14. 打包与部署补充约定

- Windows 构建在本仓库 `desktop/` 下执行，产物由 `electron-builder` 生成，证书动作只由 `desktop/scripts/sign-win.ps1` 调用外部 `signtool.exe` 完成。
- Windows 正式签名包使用 `npm run package:win:signed`，流程为：stage 核心运行时 -> build -> 生成 `win-unpacked` -> 签名 unpacked 内部 exe -> 从已签名 unpacked 生成 NSIS -> 只签最终 setup。
- Windows 安装器采用 SHA256 单签。不要恢复 SHA1+SHA256 双签；已验证这会让 NSIS 安装器出现 `HashMismatch`。
- 默认 Windows 核心运行时使用 Python embeddable，不复制本机完整 Python，避免把用户/全局 site-packages 打入安装包。
- 管理员预置能力包可通过 `desktop/scripts/stage-runtime-win.ps1 -PreinstallPacks <packId>` 完成，预置后必须重新评估安装包体积。
- 客户端内不得删除、导入、清理或管理签名证书。
- macOS DMG 必须在 macOS 构建节点生成。Windows 环境只能验证配置读取，不能作为 DMG 最终构建环境。
- macOS runtime staging 使用 `desktop/scripts/stage-runtime-mac.sh`，按 `arm64` / `x64` 下载对应 `python-build-standalone` 运行时。
- macOS 手动/CI 打包命令：
  - Apple Silicon：`npm run package:mac:arm64`
  - Intel：`npm run package:mac:x64`
  - 双架构：`npm run package:mac`
  - 无证书本地包：`npm run package:mac:unsigned`
- macOS GitHub Actions 工作流：`.github/workflows/ecorex-desktop-release.yml`，手动触发后产出 arm64/x64 DMG artifact；配置 `MAC_CERTIFICATE`、`MAC_CERTIFICATE_PASSWORD`、`APPLE_ID`、`APPLE_APP_SPECIFIC_PASSWORD`、`APPLE_TEAM_ID` 后可签名、公证并 staple。
- macOS 发布前必须执行 app bundle 结构检查、`codesign -dvvv --entitlements :-`、`spctl -a -vv`、notarization/staple 检查。
- 下载页与管理页部署在 `https://www.ecoreai.cn/ecorex-agent/` 路径下，服务器目录使用 `/srv/ecorex-agent-download/current` 软链切换 release。
- 管理页用 Caddy `basic_auth` 做入口账密保护，前端不得写死明文密码。
- Caddy/Docker 变更必须只作用于 `/ecorex-agent` 路由和只读挂载 `/srv/ecorex-agent-download`，不得改动主站 Web/MCP 路由行为。
- 每次发布必须保留旧 release 和 Caddy/compose 备份，便于回滚。
# 2026-06-10 补充：管理员模型连接配置边界

目标：
- 管理员可在 Admin Web 中为全局、用户或设备配置模型 provider、model、Base URL、API Key。
- 普通用户安装 EcoreX 桌面端后不需要手动配置模型连接，即可由桌面端拉取企业策略并开始聊天。
- 管理员修改或删除 API Key/Base URL 后，桌面端在下一次发送消息前刷新策略；如配置变化，自动重启 sidecar 后继续发送，实现近实时生效。

当前实现：
- Admin API 使用 `model_credentials` SQLite 表保存模型凭据。
- Admin Web “模型连接策略”支持新增、编辑、删除；列表只显示 masked key。
- 桌面端通过 `/ecorex-agent/client/model-config` 拉取运行时配置，并注入 Python sidecar 环境变量。
- Windows/macOS staging 脚本写入 `enterprise-policy.json`，安装包只包含企业客户端通道密钥和公开策略 URL，不包含真实模型 API Key。
- 客户端事件通过 `/ecorex-agent/client/events` 上报到 Admin API，用于用量和错误回溯。

开发边界：
- 不改 agent core 的模型调用逻辑，只通过已有 `bot_type`、`model`、`open_ai_api_key`、`open_ai_api_base` 等配置键注入。
- 不在仓库、文档、日志或前端 bundle 中记录真实模型 API Key。
- 不把 client event key 当作最终生产级安全方案；它只是 r7 内测期的企业客户端通道保护。
- 生产阶段必须补齐设备绑定、SSO、短期 token、禁用用户即时失效、审计脱敏和按部门/用户配额。

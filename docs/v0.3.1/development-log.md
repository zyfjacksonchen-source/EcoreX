# EcoreX v0.3.1 开发留痕

## 持续目标

- Goal thread: `019fd225-43bb-7ba2-bd0f-5e7202da2b96`
- Objective: 依据 `development-slices.md` 和 `acceptance-checklist.md` 完成 v0.3.1，直到全部验收门禁通过。
- Delete policy: 逻辑删除，保留审计数据。
- `@` scope: 已启用且可用的系统能力、Skill 和协作能力。
- Custom Skill: ZIP 一键导入，Hub 发布收进高级操作。
- Release: macOS 使用当前账号仓库最新 workflow，手动发布，`gh` 当次进程复用当前 UniClash 代理。
- Deployment target: 服务器连接信息只从 `C:\Users\user\Desktop\企业服务器地址.txt` 临时读取；文档和日志不落盘凭据。
- Product boundary: 只做 Windows/macOS 双端稳定 WebUI，不做独立桌面客户端；Runtime/API 作为 WebUI 运行链路的最小配套可修改。
- Package policy: 内置运行环境必须同步减包；以实际运行依赖清单为准，去重并排除开发/缓存/测试内容，裁剪后用隔离环境回归。
- Upstream baseline: CowAgent 官方仓库 `https://github.com/zhayujie/CowAgent`，审计 HEAD `866d40c5ff6b0b600b5015ec12ca821f6e468b45`；行为对照发布 `2.1.3`、`2.1.4`、`2.1.5`。

## 实施前工作树

- `scripts/build-v030-macos-universal-webui.py` 已修改（用户改动，不覆盖）。
- `scripts/smoke-v030-windows-terminal-package.ps1` 已修改（用户改动，不覆盖）。
- `tests/v1/test_macos_universal_webui_producer.py` 已修改（用户改动，不覆盖）。

## 2026-08-05 S0

- 完成需求与现有实现链路勘察：首页、Composer、项目选择、能力中心、连接器、归档任务、原生目录选择、模型目录。
- 已创建长期 Goal，已固定开发切片与验收方案。
- 产品设计上下文预检：无持久化用户上下文，使用本次截图与仓库设计 token。
- 基线测试第一次 Python 命令选错环境；已切换到仓库 `.venv`。
- 基线定向命令包含不存在的 `test_project_service.py`，本次无测试执行；下一步改用实际存在的项目/输出测试。
- 基线重跑：`npm run typecheck` 通过。
- 基线重跑：`npm run test:v1` 216/216 通过。
- 基线重跑：任务目录/模型/项目/输出 Python 定向测试 47 通过、1 跳过。

## 切片状态

| 切片 | 状态 | 验收 |
| --- | --- | --- |
| S0 | 已完成 | 类型、216 前端测试、47+1 Python 基线 |
| S1 | 已完成（视觉总验待 S9） | 类型+产品语言契约 10/10 |
| S2 | 已完成（视觉总验待 S12） | AC-02/04/06/07 |
| S3 | 已完成（联调总验待 S12） | AC-03 |
| S4 | 已完成（视觉总验待 S12） | AC-05 |
| S5 | 已完成（交互总验待 S12） | AC-13 |
| S6 | 已完成（视觉总验待 S12） | AC-12 |
| S7 | 已完成（双端实包总验待 S12） | AC-08 |
| S8 | 已完成 | AC-15/19；39 通过、2 跳过 |
| S9 | 已完成（视觉总验待 S12） | AC-16；Python 53/53、前端 219/219 |
| S10 | 已完成（飞书真实联调待 S12） | AC-17；36/36 定向回归 |
| S11 | 已完成 | AC-10；Python 157/157、前端 219/219 |
| S12 | 进行中 | AC-18 + 全量；WebUI 视觉验收已通过 |

## 2026-08-05 S1

- 首页容器改为 `min-height: 100%` + 自动高度，内容超高时不再产生不可回滚的负向裁切。
- Composer 消费模板预填后通知 App 清空一次性请求，阻止重挂载重复注入。
- 最近任务改为可聚焦按钮，调用已有 `runtime.openThread`。
- 首页项目选择删除原生 select 和隐藏按钮，复用 `NewConversationProjectSelector`，菜单内常显添加项目文件夹。
- 校验：`npm run typecheck` 通过；`product-language-contract.test.mjs` 10/10 通过。

## 2026-08-05 S2

- 设置权限改为可访问的 switch，完全访问仍保留原二次确认，默认权限可直接切回。
- 密码表单改为当前密码整行、新密码与确认密码双列；窄屏沿用单列。
- 普通任务消息图标在完整侧栏移除，紧凑侧栏保留导航识别；任务列表背景/边框统一为透明。
- 能力分类改为常显图标按钮，复用现有 Lucide 图标和能力分类数据。
- 校验：`npm run typecheck` 通过；design/extension/accessibility/layout 定向契约 21/21 通过。

## 2026-08-05 S3

- Composer 删除连接器入口；连接配置统一进入能力中心的“协作连接”。
- 能力中心复用后端连接器目录、健康状态、OAuth/配置动作，展示飞书、腾讯文档及已注册外部 IM，不维护第二份前端硬编码目录。
- 校验：`npm run typecheck` 通过；lazy/density/bundle 契约 22/22 通过。

## 2026-08-05 S4

- 本地 Skill 简化为选择 ZIP 后直接导入，标识由 Skill 内容稳定生成；保留旧客户端显式标识与版本栅栏兼容。
- Skill Hub 发布移入“高级操作”，默认界面不暴露扩展标识和内部文件名。
- 校验：扩展平台 22 通过、2 跳过；扩展管理契约 6/6 通过；`npm run typecheck` 通过。

## 2026-08-05 S5

- Runtime 新增已启用且可用的系统能力、Skill、协作能力提及目录；目录快照由后端能力与扩展状态生成。
- Composer 支持输入 `@` 搜索、键盘选择、能力标签移除；发送时将结构化引用写入可恢复 outbox，请求重试不丢失或串换能力。
- Skill 引用使用独立命名空间并校验当前贡献快照，只提升既有 `skill_search`，不绕过权限和执行治理。
- 校验：`npm run test:v1` 218/218 通过；扩展执行新增定向测试通过。

## 2026-08-05 S6

- 已归档任务行改为可点击查看；查看态隐藏 Composer 并提供“恢复后继续”。
- 归档任务管理菜单提供恢复与删除，删除前二次确认；仅 archived 可删除。
- 删除写入 `deleted` 状态和 `thread.deleted` 审计事件，不清理原消息、产物和事件；active/archived/all 目录、按 ID 打开、回放和恢复均不再暴露删除态。
- 校验：任务目录/组合/回放/Skill 联合回归 68/68 通过；`npm run test:v1` 218/218 通过；`npm run typecheck` 通过。

## 2026-08-05 S7

- 项目目录与输出目录继续复用同一 Runtime 原生目录选择器；WebUI 不接收或执行本地命令。
- Windows 目录选择与资源管理器定位均固定 `shell=False`、`DEVNULL`、`CREATE_NO_WINDOW`；打开文件使用系统关联；macOS 使用固定 `/usr/bin/osascript` 与 `/usr/bin/open`/Finder 参数。
- 校验：项目、输出、产物外部动作 30 通过、1 跳过；新增隐藏子进程和无 shell 契约测试。

## 2026-08-05 S8

- 对照 CowAgent `dd10d99`，在旧 Agent Bash 与 WebUI Capability Pack 两条实际执行链路统一解释搜索/比较命令的提示性退出码；保留真实退出码、stderr 和非预期失败。
- 浏览器表达式执行遇到顶层 `return` 时仅重试一次 IIFE 函数体；二次失败仍返回原有错误边界。
- Vision 相对路径与 `~` 统一按运行工作区解析，文件读取继续经过现有权限 broker，越界不会绕过访问策略；Bridge 对所有工具注入工作区目录。
- Web Runtime 复用现有 SSE `finally` 回收实现，新增生成器关闭计数测试；16 路通知测试继续验证重复连接无快轮询与关闭回收。
- 对照 CowAgent `0bc0f2b`，进化审查 Agent 标记为 restricted，实时 MCP 同步同时识别直接 Agent 与流式包装 Agent，禁止重新注入外部工具。
- 校验：工具/浏览器包/Sandbox 包/SSE 相关回归 39 通过、2 跳过；Python `compileall` 通过。

## 2026-08-05 S9

- 对照 CowAgent `8c7cda8`，在 WebUI 使用的 `ManagedHTTPMCPTransport` 增加 OAuth 2.1 Bearer 注入与 401 后单次刷新；未授权不会自动反复弹窗。
- 新增远程 MCP OAuth authority：RFC 9728 受保护资源发现、RFC 8414 授权服务器发现、动态客户端注册、S256 PKCE、单次 state、授权码换取、主动刷新、撤销/清除。
- 注册配置随已验证 MCP Runtime binding 下发；资源与授权主机固定到管理员允许列表，重定向只允许当前 e-Mate loopback 回调，禁止跨主机跳转。
- OAuth pending 状态绑定租户与服务；access/refresh token、动态 client secret 和元数据写入现有 Windows Credential Manager/macOS Keychain vault，不进入 SQLite、日志、目录响应或 WebUI。
- 能力中心的 MCP 扩展详情展示公开授权状态、范围、刷新、去授权与取消授权；WebUI 只接收 HTTPS 授权 URL，不采集令牌。
- 校验：OAuth/HTTP MCP/Runtime composition Python 回归 53/53；WebUI `typecheck` 通过；`test:v1` 219/219 通过。

## 2026-08-05 S10

- 对照 CowAgent `257eb5d`，复用 Card 2.0 状态归并器：单张流式卡片以折叠面板展示思考摘要、工具名/状态/耗时和总耗时；不写入工具参数，完成与中断均关闭 streaming mode。
- 对照 `9f63453`/`6e10b8e`，非流式与定时文本按 Markdown 特征选择卡片，纯文本保持轻量消息；远程图片经协议、DNS/IP、重定向、类型与 10 MiB 上限校验后上传，卡片失败自动回退文本。
- 对照 `9d8c655`，`/tasks` 只展示当前飞书接收方的任务，卡片按钮复用现有 TaskStore 执行启用、停用和删除；重复动作以当前后端状态重绘。
- 对照 `2635c12`，引用文本、富文本、图片、文件、语音和视频摘要进入本轮上下文；引用拉取失败仅降级本条引用，不阻断当前消息。
- 对照 `177f494`，消息 ID 作为 request ID 写入现有取消注册表；撤回仅移除对应排队消息或取消对应运行中请求，未知/重复撤回幂等忽略，并保留现有 ingress 去重释放。
- WebSocket 与 webhook 均接入撤回和卡片动作；回调 token 继续校验，日志沿用 HMAC 引用与响应摘要，避免凭据、消息 ID 和卡片 ID 明文落盘。
- 校验：飞书专项与 ChatChannel 回归 36/36 通过；Python 编译与 `git diff --check` 通过。旧 v0.2.4 Web 控制台测试依赖已移除的 `channel/web`，未纳入当前 WebUI 门禁。

## 2026-08-05 CowAgent 上游对照

- 官方发布说明确认：`2.1.5` 的工具返回准确性；`2.1.4` 的远程 MCP OAuth 与飞书 #2961/#2962/#2963/#2966/#2967/#2978；`2.1.3` 的 Web 连接回收 #2924 与自主进化隔离 #2904。
- 源码关键提交：`dd10d99`（退出码/浏览器函数体/图片相对路径）、`8c7cda8`（MCP OAuth）、`257eb5d`/`9f63453`/`9d8c655`/`2635c12`/`6e10b8e`/`177f494`（飞书）、`12cd626`（SSE 回收）、`0bc0f2b`（进化工具隔离）。
- 已将行为拆为 S8—S10，并把原模型与发布切片顺延为 S11—S12；新增 AC-15—AC-19。
- 移植边界：只复用行为与失败语义，落到 EcoreX 现有 MCP、网关、调度、连接器和分发链路；不引入 CowAgent 桌面壳。

## 2026-08-05 S12 范围追加

- 用户确认：检查当前发布包是否已内置运行环境；若已内置，v0.3.1 必须同步减包。
- 验证方法：分平台生成文件/体积清单，检查重复解释器与依赖，裁剪缓存、测试、头文件、静态库、源码映射和未引用依赖；减包后在无系统 Python/Node 环境跑 self-check 与功能冒烟。

## 2026-08-05 S11

- 托管模型合同端到端接受 `max`；新安装默认 `gpt-5.6-luna` + 最大推理。
- DeepSeek 保留兼容本地模型位 `ecorex-deepseek-v4-pro`，目录展示与上游固定为 V4 Flash，最大推理；旧别名继续可解析。
- 管理目录公开每个聊天模型的推理强度；Runtime 每 5 秒及窗口聚焦静默刷新目录，已选模型下线时回退服务端默认，已运行任务继续使用其冻结快照。
- 校验：模型目录/运行时组合/控制面/Provider/能力/Worker/网关 Python 分组共 157/157；WebUI `typecheck`、合同生成检查通过，`test:v1` 219/219。

## 2026-08-05 S12 运行环境审计与减包

- 旧发布物确认已内置 Python 与 Browser Node/Chromium：Windows 包 278,657,484 B（展开 462.98 MiB），macOS Universal 包 548,924,606 B；本版不再重复引入第二套运行环境。
- 在现有 staging 闭包器上统一剔除开发头文件、静态库、source map、C/C++/Cython 源文件、类型 stub、Tk 与测试专用动态库；保留 Browser、OCR、Office、Sandbox、Image 和 Channels 的实际运行闭包。
- Windows 真实自包含构建：Core 61,399,497→56,709,647 B，OCR 250,400,682→247,796,571 B，Office 35,082,034→33,366,968 B，Browser 131,008,538→130,587,233 B；展开运行内容共减少 9,430,332 B。
- Python 闭包为 477 个文件/56,709,306 B，SHA-256 `f990524edbd5109bc9fa96b6b8cb8ec75382bbc5c7d578baa107486278ed09f4`，禁入开发载荷为空。
- 真实 Windows 隔离冒烟通过 Runtime self-check、OCR 推理、Office 往返和 Browser，未使用系统 Python/Node。macOS 同源规则静态预计 arm64/x64 至少分别减少 5.53/5.55 MiB，实包数据等当前仓库 workflow 产物回填。
- 新增 Windows/macOS 分平台展开总量、runtime 和分组件上限；超限时同时打印 Top-20 大文件。候选发布门禁也校验 core 包上限。
- 完整字节证据：`docs/v0.3.1/artifacts/ac-18-runtime-size-audit.json`。
- 当前版本源、WebUI、发布站点和当前 workflow 合同已升级到 `0.3.1`；历史迁移和候选基线中的旧版本字面量保留。
- WebUI 生产首屏在不放宽预算的前提下将 Composer 放入已有延迟功能边界，共享附件预览收敛到既有 UI primitives chunk；修正 bundle gate 使其支持“延迟 chunk 引用下一级延迟 chunk”的真实 DAG。
- 校验：生产内容哈希 38 assets 通过；首屏 JS 468.04 KiB（gzip 148.74 KiB）低于 475/150 KiB 门禁；WebUI `typecheck`、`test:v1` 221/221、生产构建通过。

## 2026-08-06 S12 WebUI 视觉验收

- 以用户截图和同状态实现截图生成首页、项目选择、密码、任务列表、最近任务五组并排对照；结论记录在 `design-qa.md`。
- 真实浏览器完成项目选择、最近任务导航、归档查看/恢复/逻辑删除、`@` 能力选择、九类能力筛选、外部 IM 目录、Skill 简化导入和权限/密码设置交互。
- 320×568 与 1440×900 均无横向溢出；1440×900 修复趋势图 ARIA 角色后验收报告 `passed: true`，axe 0 violation、36 passes。
- GA mock 补齐归档/恢复/逻辑删除、能力提及和后端外部 IM 目录，相关前端总回归 221/221。
- Playwright 发布门禁新增 641×249、1366×768、1920×1080 首页检查：机器人、标题和 Composer 逐项滚动到视口后必须完整可见，且无横向溢出；候选 workflow 将再次执行该门禁。
- 生产 Luna 验收探针原先请求 `high`、证据却写 `max`，现统一实际请求和证据为 `max`，并新增生成程序合同测试，避免验收误报。
- 首次 Python 全量回归：2628 通过、60 跳过、2 失败。OCR 失败源于测试把宿主预存模块错误写死为“不存在”，已改为验证调用前后对象一致；Windows 快捷方式读取补充显式 UTF-8 和一次无延迟 COM 重试，并加入确定性回归。
- 修复后最终 Python 全量回归：2631 通过、60 跳过、零失败，耗时 1640.13 秒（27:20）；标准错误为空。

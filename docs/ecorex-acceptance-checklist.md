# EcoreX 真实用户验收 Checklist

本文档不是代码层 smoke test，而是模拟真实用户、管理员、IT 运维的完整使用验收。每个阶段交付后都要按本清单抽样跑一遍，避免“代码能跑但用户不可用”。

## 1. 验收角色

| 角色 | 关注点 |
| --- | --- |
| 普通用户 | 安装后能否直接用、聊天是否可靠、文件是否安全、权限是否清楚 |
| 高级用户 | Skill/MCP、文件批处理、联网研究、多任务并发 |
| 企业管理员 | 用户创建、策略、用量、错误日志、审计 |
| IT 运维 | 安装包、更新、日志、崩溃诊断、回滚 |

## 2. 验收等级

| 等级 | 定义 | 是否阻断发布 |
| --- | --- | --- |
| P0 | 数据丢失、越权、无法启动、核心功能不可用 | 阻断 |
| P1 | 常用流程失败、明显卡死、权限误导 | 阻断 |
| P2 | 体验瑕疵、文案不清、非核心边界问题 | 可带记录灰度 |

## 3. 安装与开箱即用

- [ ] Windows 用户安装后能打开 EcoreX。
- [ ] macOS 用户安装后能打开 EcoreX，未被 Gatekeeper 阻止。
- [ ] Windows 普通用户不需要安装 Python、Node、Git 或其他运行时依赖。
- [ ] macOS 普通用户不需要安装 Python、Node、Git 或其他运行时依赖。
- [ ] 用户不需要打开命令行启动 CowAgent 或 sidecar。
- [ ] 用户不需要手动配置本地端口、CowAgent 路径或工作区路径。
- [ ] 首次启动显示 EcoreX 名称和图标。
- [ ] 用户登录或组织绑定后自动获得企业配置。
- [ ] 无需用户手动配置 CowAgent 路径即可开始聊天。
- [ ] 企业策略、模型、通道、Skill/MCP 模板能在首次登录后自动同步。
- [ ] 首次启动失败时能显示用户可理解的修复建议，而不是只给技术堆栈。
- [ ] 普通用户权限下可完成安装和启动，除系统要求外不依赖管理员手动干预。
- [ ] Windows 重启后应用、更新器和 sidecar 状态正常。
- [ ] macOS 重启后应用、更新器和 sidecar 状态正常。
- [ ] macOS arm64 DMG 由 macOS runner 生成，文件名、版本、hash 写入 manifest。
- [ ] macOS x64 DMG 由 macOS runner 生成，文件名、版本、hash 写入 manifest。
- [ ] macOS `.app` 内包含 `Contents/Resources/ecorex-runtime/python/bin/python3`。
- [ ] macOS `.app` 内包含 `capabilities.json` 和 `scripts/install-capability.py`。
- [ ] macOS DMG 通过 `codesign --verify --deep --strict`。
- [ ] macOS DMG 通过 `spctl` Gatekeeper 检查。
- [ ] macOS DMG 已 notarize 并通过 `xcrun stapler validate`。
- [ ] 离线状态下能看到清晰提示，不误报为系统损坏。
- [ ] 退出应用后 sidecar 正常退出，没有残留大量进程。

## 4. 首次使用能力包安装

- [ ] 默认安装包不包含 Slack/Discord/Telegram/WeChat/DingTalk、语音、Playwright、Office/PDF、numpy/pandas 等重型依赖。
- [ ] 用户请求解析 PDF/Office 文件时，EcoreX 自动识别需要 Office/PDF 能力包。
- [ ] 用户请求浏览器自动化或网页点击时，EcoreX 自动识别需要 Playwright 能力包。
- [ ] 用户请求 Slack/Discord/Telegram/WeChat/DingTalk 等通道时，EcoreX 自动识别需要 IM 通道能力包。
- [ ] 用户请求语音输入、音频转写或 TTS 时，EcoreX 自动识别需要语音能力包。
- [ ] 用户请求通义千问、智谱、Gemini 等厂商 SDK 模型时，EcoreX 自动识别需要模型厂商 SDK 能力包。
- [ ] 缺失能力时，chat 中出现 human-in-the-loop 卡片，而不是直接失败。
- [ ] 卡片用用户语言说明需要安装什么、为什么需要、预计体积。
- [ ] 用户点击“安装并继续”后，安装成功会自动继续原任务。
- [ ] 用户点击“跳过继续”后，EcoreX 会尝试执行并在失败时给出可理解原因。
- [ ] 用户点击“取消”后，不安装能力包，也不继续执行原任务。
- [ ] 安装失败时展示能力包名称、失败原因、日志入口和重试路径。
- [ ] 安装失败不会导致桌面端崩溃，普通文字对话仍可继续。
- [ ] Settings 中可查看每个能力包的已安装/未安装/失败状态。
- [ ] Settings 中可手动安装能力包，供高级用户或 IT 运维处理。
- [ ] 管理员预置能力包后，普通用户首次使用相关能力不再触发下载。
- [ ] macOS 首次安装能力包写入用户数据目录，不修改 `.app/Contents/Resources`。
- [ ] macOS 能力包安装后，sidecar 能通过 `PYTHONPATH` 找到新模块。
- [ ] 企业网络禁止访问 PyPI 时，有清晰提示指向管理员预置或镜像源。
- [ ] 能力包安装状态和日志可被 Admin Web 或诊断包读取。

## 5. 视觉与主题

- [ ] 主品牌色为橙色。
- [ ] Light 模式可用。
- [ ] Dark 模式可用。
- [ ] System 模式能跟随系统。
- [ ] 切换主题后聊天、文件预览、设置、弹窗都正确。
- [ ] 组件颜色、圆角、阴影来自 token。
- [ ] 主界面符合 Codex 桌面端式布局：左侧 session/项目上下文侧栏，右侧 chat 主工作区。
- [ ] 主界面没有被做成两个等权业务列。
- [ ] 技术检查器没有常驻为第三主列。
- [ ] 文件预览默认不显示，点击文件后才出现。
- [ ] hover 明细使用用户能懂的话，不堆技术语言。
- [ ] 图标按钮 hover 有 tooltip。
- [ ] 圆角风格统一，没有突兀硬方块。

## 6. 会话与聊天

- [ ] 新建会话成功。
- [ ] 切换会话后上下文不串。
- [ ] 会话列表能看到当前状态：空闲、运行中、等待确认、失败。
- [ ] EcoreX 回复身份正确，不显示 CowAgent。
- [ ] 用户能停止正在运行的回复。
- [ ] 停止后任务不会继续后台消耗用量。
- [ ] 同一问题不会无限循环调用工具。
- [ ] 达到最大步骤时能主动停下并说明原因。
- [ ] 网络失败、模型失败、工具失败时能给出可行动提示。
- [ ] 长会话不会因消息太多导致明显卡顿。

## 7. human-in-the-loop

- [ ] 高风险动作前出现 human-in-the-loop 卡片。
- [ ] 卡片说明 EcoreX 准备做什么。
- [ ] 卡片说明影响范围，例如目录、文件、外部通道。
- [ ] 用户能选择允许本次。
- [ ] 用户能选择始终允许同类动作。
- [ ] 用户能选择拒绝。
- [ ] 用户能查看明细。
- [ ] 用户拒绝后 Agent 能继续给出替代方案。
- [ ] 确认节点不会被工具日志淹没。
- [ ] 普通低风险动作不会频繁打扰用户。

## 8. 权限与安全边界

- [ ] 用户可选择权限模式：Smart Ask、Always Ask、Read Only、Custom。
- [ ] 读取工作区内普通文件不频繁弹窗。
- [ ] 访问工作区外目录需要授权。
- [ ] 删除文件需要确认。
- [ ] 覆盖文件需要确认。
- [ ] 执行 shell/cmd 需要策略允许和用户确认。
- [ ] 外发文件到飞书等通道需要确认。
- [ ] 被管理员禁用的能力在 UI 中清楚标注。
- [ ] 权限选择会进入审计日志。
- [ ] 用户能撤销“始终允许”的权限。

## 9. 文件能力

- [ ] 上传单个文件成功。
- [ ] 上传文件夹成功，能保留结构。
- [ ] 点击图片后出现预览。
- [ ] 点击视频后出现预览。
- [ ] 点击 PDF 后出现预览。
- [ ] 点击 Markdown/文本/代码后出现预览。
- [ ] Office 文件第一阶段能显示文本抽取和 metadata。
- [ ] 用户能用系统应用打开原文件。
- [ ] Agent 能读取授权文件并总结。
- [ ] Agent 能写入新文件。
- [ ] Agent 能编辑文件并展示 diff。
- [ ] 删除或覆盖必须经过确认。
- [ ] 文件路径不会泄露到不必要的外部消息。

## 10. 联网搜索与网页抓取

- [ ] 用户发起联网搜索后能看到“搜索中”状态。
- [ ] 搜索结果有来源摘要。
- [ ] 搜索失败时提示 provider 或网络问题。
- [ ] 管理员禁用联网后，普通用户无法绕过。
- [ ] web_fetch 能读取普通网页。
- [ ] 远程 PDF/Office 文档过大时有提示。
- [ ] 联网引用不会伪造来源。

## 11. Skill 验收

- [ ] Settings 中能进入 Skill 页面。
- [ ] 能浏览可安装 Skill。
- [ ] 能从允许来源安装 Skill。
- [ ] 未授权来源安装会被拦截。
- [ ] 安装后能看到启用状态。
- [ ] 用户能禁用 Skill。
- [ ] Agent 能在相关任务中发现该 Skill。
- [ ] Agent 能正常调用该 Skill。
- [ ] Skill 调用失败时能看到失败原因。
- [ ] Skill 安装、启用、调用进入审计。
- [ ] `compatSource=cowagent` 下旧 Skill 仍可用。
- [ ] `ecorex` namespace alias 不破坏旧 namespace。

## 12. MCP 验收

- [ ] Settings 中能进入 MCP 页面。
- [ ] 能添加 stdio MCP server。
- [ ] 能添加 SSE/HTTP MCP server。
- [ ] MCP 连接状态清楚。
- [ ] MCP tool 能被发现。
- [ ] Agent 能调用 MCP tool。
- [ ] MCP server 失败时能看到日志入口。
- [ ] 未授权 MCP 不会自动启动。
- [ ] 管理员禁用 MCP 后普通用户不可绕过。
- [ ] MCP 调用进入审计。
- [ ] MCP 空闲后能释放资源。

## 13. 设置入口

- [ ] 主界面只有一个清晰 Settings 入口。
- [ ] Models 在 Settings 内。
- [ ] Channels 在 Settings 内。
- [ ] Skills 在 Settings 内。
- [ ] MCP 在 Settings 内。
- [ ] Permissions 在 Settings 内。
- [ ] Files 在 Settings 内。
- [ ] Diagnostics 在 Settings 内。
- [ ] 能力包状态和安装入口在 Settings 内。
- [ ] 普通用户看不到无权限的管理员复杂配置。

## 14. 管理员 Web

- [ ] 管理员能创建用户。
- [ ] 管理员能禁用用户。
- [ ] 管理员能调整角色。
- [ ] 管理员能查看设备列表。
- [ ] 管理员能查看用户用量。
- [ ] 管理员能按部门查看用量。
- [ ] 管理员能查看模型用量。
- [ ] 管理员能查看 Skill/MCP 调用量。
- [ ] 管理员能按用户、设备、版本、会话查询错误日志。
- [ ] 错误日志能回溯到具体工具调用。
- [ ] 管理员能下发权限策略。
- [ ] 管理员能配置飞书等连接器模板。
- [ ] 管理员能配置能力包预置策略、镜像源或离线包缓存。
- [ ] 管理员能查看审计日志。
- [ ] 管理员操作也进入审计。

## 15. 多 Agent 并发

- [ ] 用户能同时启动多个独立任务。
- [ ] 左侧能看到每个 Agent 的状态。
- [ ] 用户能停止单个 Agent。
- [ ] 用户能停止全部任务。
- [ ] 多 Agent 不会互相覆盖文件。
- [ ] 多 Agent 不会修改 goal 外文件。
- [ ] 任务队列满时有清晰提示。
- [ ] 后台任务不会让 chat 输入明显卡顿。
- [ ] 每个 Agent 有明确任务边界。
- [ ] Goal Ledger 记录了并发任务产物和决策。

## 16. 性能与稳定性

- [ ] 冷启动在目标时间内进入可交互状态。
- [ ] 打开 20 个会话列表不卡顿。
- [ ] 100 条以上消息滚动不卡顿。
- [ ] 大文件预览不会冻结主界面。
- [ ] MCP server 多个配置时按需启动。
- [ ] Electron renderer 内存持续上涨时有诊断线索。
- [ ] 崩溃后重启能恢复最近会话。

## 17. 品牌兼容

- [ ] 用户可见处显示 EcoreX。
- [ ] 飞书 bot 外显名称为 EcoreX。
- [ ] 聊天身份为 EcoreX。
- [ ] 应用图标为 EcoreX。
- [ ] 安装包名称为 EcoreX。
- [ ] 内部 `compatSource=cowagent` 仍能路由旧能力。
- [ ] `cow` CLI 仍可用。
- [ ] `ecorex` CLI alias 可用。

## 18. 遗漏点补充验收

- [ ] 企业 SSO 或邀请码流程可走通。
- [ ] 用户离职或禁用后桌面端无法继续访问企业能力。
- [ ] 设备绑定和解绑可追踪。
- [ ] 企业代理环境下能启动、登录、联网或给出明确提示。
- [ ] 离线时能使用允许的本地能力，并清楚标注不可用能力。
- [ ] 日志和诊断包会脱敏用户文件内容、密钥、token。
- [ ] 用户能导出诊断包给 IT 运维。
- [ ] prompt injection 场景不会诱导 Agent 越权外发文件。
- [ ] 网页内容中的恶意指令不会覆盖系统策略。
- [ ] 未签名或未知来源 Skill/MCP 会被拦截或强确认。
- [ ] 自动更新失败后能回滚到上一可用版本。
- [ ] `~/cow` 历史工作区迁移到 EcoreX 时不丢数据。
- [ ] 用户能选择不迁移，并继续使用兼容路径。
- [ ] 键盘可完成主要操作：切换会话、发送、停止、打开设置、关闭预览。
- [ ] 焦点态清晰，明暗模式对比度可读。
- [ ] 管理员可配置用户或部门用量配额。
- [ ] 异常循环会触发熔断，不持续消耗预算。
- [ ] 后台任务完成、失败、等待确认都有通知或状态提示。

## 19. 发布前结论

发布前必须填写：

```text
验收日期：
验收版本：
验收环境：
验收角色：
P0 数量：
P1 数量：
P2 数量：
是否允许灰度：
阻断项：
风险接受人：
下一步：
```

## 20. 验收执行记录

### 2026-06-10 Desktop Foundation

```text
验收范围：desktop 子工程基础构建、token 约束、Electron sidecar 管理层、WebChannel 桌面模式兼容、白名单 IPC API bridge、运行时快照、最小聊天发送/流式接收。
已通过：
- npm run typecheck
- npm run build
- npm audit --audit-level=critical
- python -m py_compile channel/web/web_channel.py
- 搜索确认 UI 颜色硬编码只保留在 tokens.css
- renderer 能通过 IPC 调用允许的 sidecar API 路径
- UI 已接入 sessions/tools/skills/models/version 快照并保留 fallback
- composer 已接入 /message 与 /stream 的最小聊天闭环
未完成：
- 未做浏览器截图验收，原因是本轮 in-app browser 控制工具未暴露。
- 未完成 Windows/macOS 安装包和内置运行时，开箱即用验收仍未通过。
- 未完成真实历史消息、文件上传/预览、Skill/MCP 设置操作和 Admin Web。
结论：Phase 1/Phase 3 基础工程有进展，但整体产品验收未完成，goal 保持 active。
```

### 2026-06-10 Windows Package And Download Center

```text
验收范围：Windows 0.1.4 安装包构建与外部签名、EcoreX 图标打包、双端下载页面、管理员页面 Basic Auth、服务器 EcoreX 路径隔离部署。
已通过：
- npm run build
- npm run package:win:signed
- Get-AuthenticodeSignature 显示安装器、EcoreX.exe 均为 Valid
- Windows 安装器本地最终体积 117522648 bytes / 约 112.1MB，SHA256 7DE777D01CA84418276A48CE2F3D3F69664527AF2E9CEE3B49380BBC6611645C
- Windows 安装器本机真实安装烟测通过：静默安装到临时目录、启动已安装 EcoreX.exe、sidecar /auth/check 返回 success、卸载并清理成功
- Windows 安装器公网下载路径 HTTP 200，Content-Length 117522648，下载后 SHA256 与本地一致
- win-unpacked/EcoreX.exe 和已安装 EcoreX.exe 启动后 sidecar /auth/check 返回 200
- 能力包列表可识别 feishu-lark、office-pdf、browser-automation、voice、im-channels、memory-heavy、model-connectors 为 not-installed 并列出缺失模块
- Office/PDF 能力包首次安装测试成功，状态文件写入 installed 和日志路径
- 未知能力包安装测试返回 failed 状态，桌面端不崩溃
- 下载中心页面 HTTP 200
- manifest.json HTTP 200 且 JSON 可解析
- 管理端未登录 HTTP 401
- 管理端带账号密码 HTTP 200
- 管理端已发布能力包预置策略页签，可展示镜像源、默认策略、离线缓存和建议预置包
- macOS arm64/x64 DMG 下载路径 HTTP 200
- 主站根路径仍为原业务 302 /login，EcoreX 路由未影响其他项目
部分通过：
- macOS 下载项可用，但本轮未在 macOS 构建节点重新生成 0.1.4 DMG，页面清单已标注为 existing-server-artifact。
- macOS runtime staging、package scripts、GitHub Actions 手动工作流已补齐，但尚未实际产出 0.1.4 DMG。
- Admin Web 已有受保护页面并接入 SQLite Admin API，可创建用户、禁用/启用用户、更新角色、查看用量、写入/标记错误日志、保存能力包策略。
未通过/未执行：
- Windows 干净普通用户机器安装验收未执行；当前只完成本机临时目录真实安装烟测。
- Windows 安装后免 Python/Node/Git 的干净机器验收未执行；当前本地打包产物已内置核心 runtime。
- 能力包企业镜像源、离线包缓存、管理员预置策略已可在 Admin Web 保存；尚未由桌面端自动拉取并应用。
- macOS Gatekeeper、codesign、spctl、notarization、staple 未执行。
- Skill/MCP 真实安装、启用、发现、调用闭环未执行。
- 文件读写改删真实安全确认闭环未执行。
结论：下载与签名发布链路可用，但产品级“Win/Mac 开箱即用”仍未最终通过。
```
# 2026-06-10 补充验收：管理员模型连接策略

- [x] 管理员可配置模型 provider、model、Base URL、API Key。
- [x] 管理员列表页不回显完整 API Key，只显示 mask。
- [x] 管理员可修改已存在模型策略的 model/Base URL/API Key。
- [x] 管理员可删除模型策略。
- [x] 桌面端安装包不内置真实模型 API Key。
- [x] 桌面端安装包包含企业策略 URL，并能通过 client model-config 接口拉取模型配置。
- [x] client model-config 接口无企业客户端密钥时返回 403。
- [x] client model-config 接口带企业客户端密钥时返回当前模型策略。
- [x] 管理员修改策略后，桌面端下一次发送消息前刷新策略并在变更时重启 sidecar。
- [x] client events 接口带企业客户端密钥可写入用量/错误事件。
- [x] client events 接口不返回管理员完整 state。
- [ ] 生产级设备绑定、SSO、短期 token、用户禁用即时失效已完成。
- [ ] 管理员按用户/设备/部门下发不同模型策略已完成真实用户验收。
- [ ] 干净 Windows 普通用户机器安装后，使用管理员下发模型配置完成首条真实聊天。
- [ ] macOS 0.1.4 签名公证 DMG 安装后，使用管理员下发模型配置完成首条真实聊天。

# EcoreX 开发留痕与运维文档

本文档用于记录 CowAgent 改造为 EcoreX 过程中的关键决策、改动痕迹、排障线索、风险和后续维护入口。后续开发会以 goal 长任务推进，本文件必须作为长期记忆锚点维护，避免上下文压缩或多 Agent 并发后跑偏。

## 1. 使用规则

- 每次功能开发、修复、架构调整后都追加记录。
- 每条记录必须包含日期、改动范围、涉及文件、验证方式、风险和回滚方式。
- 涉及 `cowagent` 到 `ecorex` 的命名、source、workspace、Skill/MCP 兼容改动，必须写入兼容决策表。
- 涉及文件系统、命令执行、外部发送、MCP、Skill 安装的改动，必须写入安全与审计记录。
- 涉及 UI 方向变化，必须写入产品决策记录。
- 每个 goal 开始、暂停、完成、中断前，必须更新 Goal Ledger。

## 2. 当前产品决策快照

日期：2026-06-10

当前决定：

- 桌面端采用 Electron，不采用 Tauri。
- 后端不重写为 TypeScript，Python agent core 保持最小改动。
- 外显品牌为 EcoreX，内部兼容 source 保留 `cowagent`。
- 前端主品牌色为橙色。
- UI 支持 Light / Dark / System。
- 主界面采用 Codex 桌面端式双列：左 session/项目上下文侧栏，右 chat 主工作区。
- 文件预览点击文件后出现，不常驻占用右栏。
- Skill、MCP、模型、通道、权限统一收进 Settings。
- 管理员能力单独做 Admin Web。
- 真实用户验收必须覆盖 Skill、MCP、联网、文件、权限、human-in-the-loop、多 Agent。
- Windows/macOS 用户必须开箱即用：安装后登录或绑定组织即可使用，无需命令行、Python、Node、Git、手动端口或 CowAgent 路径配置。
- 默认安装包只带核心运行时，Slack/Discord/Telegram/WeChat/DingTalk、语音、Playwright、Office/PDF 重型解析、模型厂商 SDK 等作为能力包首次使用安装或管理员预置。
- 普通用户不需要知道该装什么能力包，EcoreX 在任务发送前根据意图和附件自动识别缺失能力，并通过 human-in-the-loop 请求授权安装。

## 3. 当前架构快照

当前项目能力基线：

- Python package 名称：`cowagent`。
- CLI 入口：`cow`。
- 默认工作区：`~/cow`。
- Web Console：`channel/web/chat.html`、`channel/web/static/js/console.js`、`channel/web/static/css/console.css`。
- Web channel：`channel/web/web_channel.py`。
- Feishu channel：`channel/feishu/feishu_channel.py`。
- Tool 管理：`agent/tools/tool_manager.py`。
- MCP client：`agent/tools/mcp/mcp_client.py`。
- Skill Hub 默认 API：`https://skills.cowagent.ai/api`。

当前重要能力：

- 多渠道接入：Web、Feishu/Lark、DingTalk、WeCom、Slack、Telegram、Discord、QQ 等。
- 文件工具：读、写、编辑、列目录、发送文件。
- 联网能力：web_search、web_fetch、browser automation。
- MCP 能力：stdio、SSE、Streamable HTTP。
- Skill 能力：Skill Hub、Git/GitHub/GitLab、本地路径、URL 等安装来源。
- Web 文件预览：图片、视频、上传文件、`/api/file` 受控访问、Memory/Knowledge 文件读取。

## 4. 兼容决策表

| 决策项 | 当前值 | 目标值 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| 外显产品名 | CowAgent | EcoreX | UI、窗口、安装包、bot 昵称 | 待实现 |
| 聊天身份 | CowAgent | EcoreX | 用户看到的 assistant 身份 | 待实现 |
| 主品牌色 | 未统一 | 橙色 | token 化管理 | 待实现 |
| 主题模式 | 未统一 | Light/Dark/System | 用户自由切换 | 待实现 |
| 内部 source | cowagent | cowagent | 兼容外部 Skill/MCP/channel 路由 | 保留 |
| 新 product slug | 无 | ecorex | 新配置和云端路径 | 待实现 |
| CLI | cow | cow + ecorex | 保留旧 CLI，新建 alias | 待实现 |
| 工作区 | ~/cow | ~/.ecorex + ~/cow 兼容 | 迁移期双路径 | 待实现 |
| Skill namespace | cowagent/openclaw | ecorex/cowagent/openclaw | 增加 alias | 待实现 |
| MCP clientInfo | CowAgent | EcoreX + compat | 外显 EcoreX，兼容字段保留 | 待实现 |

## 5. 架构决策记录

### ADR-0001：选择 Electron 而非 Tauri

日期：2026-06-10

决策：

- 桌面端采用 Electron。

理由：

- Electron 内置 Chromium，Windows/macOS 渲染一致性更可控。
- Tauri 依赖系统 WebView，跨平台字体、滚动、输入法、文件拖拽、PDF/媒体预览可能出现差异。
- 企业桌面 Agent 更重视稳定一致和可诊断。

### ADR-0002：保留 Python Agent Core

日期：2026-06-10

决策：

- 第一阶段不把后端改写为 TypeScript。
- TypeScript 用于 Electron、React UI、Admin Web。

理由：

- 当前核心能力沉淀在 Python tools、skills、MCP、channels。
- 重写会带来长期回归风险。

### ADR-0003：外显 EcoreX，内部保留 cowagent source

日期：2026-06-10

决策：

- 对用户展示 EcoreX。
- 对兼容路由保留 `compatSource=cowagent`。

理由：

- 外部 Skill、MCP、channel 鉴权或路由可能依赖 `cowagent`。
- 直接替换会导致不可预测的断链。

### ADR-0004：文件操作必须企业治理

日期：2026-06-10

决策：

- 文件读写、删除、外发、命令执行必须可被企业策略控制并进入审计。

### ADR-0005：橙色品牌与 token 化主题

日期：2026-06-10

决策：

- EcoreX 使用橙色作为主品牌色。
- UI 必须支持 Light / Dark / System。
- 颜色、圆角、阴影、动效全部从 token 读取。

### ADR-0006：主界面采用 Codex 桌面端式布局，文件预览按需出现

日期：2026-06-10

决策：

- 主界面左侧为 session、项目上下文、最近文件、活跃任务和运行状态。
- 主界面右侧为 chat 主工作区。
- 这不是两个等权业务列，也不是常驻三栏技术控制台。
- 文件预览点击后出现，不常驻为第三列。

### ADR-0007：管理员页面独立为 Web 管理台

日期：2026-06-10

决策：

- 用户创建、用量监控、错误日志回溯、策略和审计放在 Admin Web。
- 桌面端只保留普通用户所需入口。

### ADR-0008：多 Agent 并发必须有 Goal Ledger

日期：2026-06-10

决策：

- 允许多 Agent 并发协作，但每个 Agent 必须有明确任务租约。
- 每个 goal 必须维护 Goal Ledger，记录目标、边界、决策、产物、验证和下一步。

### ADR-0009：核心运行时瘦身，重型能力包按需安装

日期：2026-06-10

决策：

- Windows 默认安装包使用 Python embeddable + `core-requirements.txt`。
- 不再复制开发机完整 Python 环境，避免把无关 user/global site-packages 打入安装包。
- 飞书、长尾 IM、语音、Playwright、Office/PDF、numpy/pandas、模型厂商 SDK 进入 `capabilities.json` 能力包。

理由：

- 完整 Python 环境曾让 staged runtime 约 663MB、安装包约 288MB。
- 核心运行时瘦身后 staged runtime 约 57.8MB、当时 Windows setup 约 112.3MB；当前发布包大小见第 17 节。
- 重型能力只有在用户任务需要或管理员预置时才安装，更符合企业桌面开箱即用和体积控制。

### ADR-0010：Agent 需要主动识别缺失能力，而不是只让用户手动安装

日期：2026-06-10

决策：

- Settings 保留能力包管理入口，但普通用户流程不依赖用户主动寻找按钮。
- 发送任务前，Renderer 根据用户文本和附件扩展名做能力预判。
- 命中缺失能力时，在聊天流中展示 human-in-the-loop 卡片：安装并继续、跳过继续、取消。
- 安装状态、失败原因和日志入口结构化写入 `capability-state`，供桌面端和未来 Admin Web 读取。

理由：

- 多数用户不会知道“解析 PDF 要装 office-pdf”或“网页自动化要装 Playwright”。
- 自动识别能减少首次使用失败，同时保留安全边界，避免静默安装。

### ADR-0011：macOS DMG 必须由 macOS runner 生成，能力包写入用户目录

日期：2026-06-10

决策：

- Windows 环境不再尝试直接生成 Electron macOS DMG。
- 仓库新增 macOS runtime staging 脚本和 GitHub Actions 手动 release 工作流。
- macOS core runtime 使用 `python-build-standalone` 的 arm64/x64 构建。
- macOS 首次安装能力包时写入 `app.getPath("userData")/capabilities/python-site`，不修改已签名 `.app` 的 `Contents/Resources`。

理由：

- Electron builder 明确限制 macOS 产物需要 macOS 构建环境。
- 修改已签名 `.app` 内部资源会带来权限、签名失效和 Gatekeeper 风险。
- 用户目录能力包配合 `PYTHONPATH` 能同时满足首次安装、管理员预置、签名完整性和后续更新。

## 6. Goal Ledger 模板

每个长 goal 开始时复制本模板到下方追加。

```text
Goal ID：
开始时间：
目标：
不做范围：
关键边界：
当前阶段：
参与 Agent：
任务分工：
共享产物：
已完成：
进行中：
阻塞：
已做决策：
需要用户确认：
验证记录：
风险：
下一步：
最后更新时间：
```

### Goal Ledger：G-2026-06-10-desktop-foundation

```text
Goal ID：G-2026-06-10-desktop-foundation
开始时间：2026-06-10
目标：按 EcoreX 开发文档开始实现桌面端基础工程，保持 agent core 最小改动。
不做范围：不重写 agent core；不删除旧 Web Console；不替换内部 compatSource。
关键边界：Codex 桌面端式布局；橙色 token；明暗模式；Settings 收口；Windows/macOS 开箱即用作为最终验收标准。
当前阶段：Phase 1 Electron POC + Phase 3 UI 骨架 + Phase 4 功能对齐 + Phase 7 打包链路推进。
参与 Agent：主 Codex 实例。
任务分工：本轮由主实例完成仓库审计、desktop 子工程、sidecar 管理层、UI token 和构建验证。
共享产物：desktop/ 子工程；docs/ecorex-*.md；desktop/README.md。
已完成：新增 Electron + React + TypeScript 工程；新增 Codex 式双列工作台；新增 Light/Dark/System token；新增 Settings Center；新增 File Preview Drawer；新增 human-in-the-loop 卡片；新增 Python sidecar lifecycle manager；WebChannel 在 ECOREX_DESKTOP=1 时不自动打开旧浏览器；新增白名单 IPC API bridge；接入 runtime snapshot；接入最小聊天发送和 SSE 流式接收。
进行中：Skill/MCP 设置操作、桌面端遥测上报、macOS 0.1.4 DMG 在 macOS runner 上实际产出与部署。
阻塞：macOS DMG 需要 macOS 构建节点和签名/公证凭据；浏览器截图工具本轮未暴露，尚未做视觉截图验收。
已做决策：desktop shell 默认尝试启动内置 Python runtime；可用 ECOREX_SKIP_SIDECAR=1 做 UI-only 开发；sidecar 启动失败不拖垮 UI；重型能力包按需安装或管理员预置；macOS 能力包装入用户数据目录而不是 app bundle。
需要用户确认：后续是否优先补真实 Skill/MCP 设置闭环、macOS runner 产物，还是先接桌面端设备/遥测上报。
验证记录：npm install；npm run typecheck；npm run build；npm audit --audit-level=critical；python -m py_compile channel/web/web_channel.py；搜索确认非 token 文件没有颜色字面量；npm run package:win:signed；打包产物 sidecar `/auth/check` 200；macOS staging/validation Bash 脚本语法检查通过。
风险：macOS 0.1.4 DMG 尚未在 macOS runner 实际生成；当前桌面 UI 仍有部分 demo 内容；Admin Web 已有 SQLite 后端但尚未接桌面端设备/遥测自动上报。
下一步：触发 `.github/workflows/ecorex-desktop-release.yml` 生成 macOS arm64/x64 DMG；接入真实 Skill/MCP 设置操作；补桌面端设备/错误/用量上报。
最后更新时间：2026-06-10
```

## 7. 多 Agent 协调记录模板

```text
日期：
Goal ID：
Agent 名称：
任务租约：
允许修改范围：
禁止修改范围：
输入上下文：
输出产物：
验证方式：
是否偏离目标：
是否需要人工确认：
交接说明：
```

## 8. 改动记录模板

```text
日期：
负责人：
分支/提交：
改动类型：Feature / Fix / Refactor / Docs / Build / Security / UI / Admin
改动范围：
涉及文件：
背景：
实现摘要：
兼容影响：
安全影响：
UI 影响：
验证方式：
验证结果：
已知风险：
回滚方式：
后续 TODO：
```

### 改动记录：Desktop Foundation

```text
日期：2026-06-10
负责人：Codex
分支/提交：未提交
改动类型：Feature / UI / Build / Docs
改动范围：新增 desktop 子工程；最小修改 WebChannel 桌面模式自动打开浏览器行为；更新开发留痕。
涉及文件：desktop/**；channel/web/web_channel.py；.gitignore；docs/ecorex-dev-log.md；docs/ecorex-acceptance-checklist.md。
背景：进入“根据开发文档开发并最终验收”的长期 goal，需要先建立 Electron + React 桌面壳和可验证构建基础。
实现摘要：新增 Electron main/preload、Python sidecar manager、React 工作台、橙色 token、明暗模式、Settings、文件预览 drawer、human-in-the-loop 卡片；新增白名单 IPC API bridge；renderer 可读取 sessions/tools/skills/models/version；composer 可通过 `/message` 发送并通过 `/stream` 接收最小流式回复。
兼容影响：WebChannel 仅在 ECOREX_DESKTOP=1 时跳过 webbrowser.open，普通 CowAgent 启动行为不变。
安全影响：sidecar 使用 windowsHide；外部链接由系统浏览器打开；UI 中高风险动作以确认卡片表达。
UI 影响：建立 Codex 桌面端式布局，不再是常驻三栏技术控制台。
验证方式：npm run typecheck；npm run build；npm audit --audit-level=critical；python -m py_compile channel/web/web_channel.py；颜色硬编码检索。
验证结果：全部通过，npm audit critical 为 0。
已知风险：未做截图验收；未接真实会话 API；未完成 Windows/macOS 打包和内置运行时。
回滚方式：移除 desktop/；还原 channel/web/web_channel.py 中 ECOREX_DESKTOP 判断；还原 .gitignore desktop 构建目录。
后续 TODO：API adapter、真实 session/file/Skill/MCP 接入、Admin Web、打包签名与开箱即用。
```

### 改动记录：Runtime Capabilities And Signed Package

```text
日期：2026-06-10
负责人：Codex
分支/提交：未提交
改动类型：Feature / Build / Security / UI / Docs
改动范围：可选能力包、轻量运行时、Electron IPC、Settings 能力包 UI、Agent 发送前能力预判、Windows 两阶段签名打包。
涉及文件：desktop/runtime-packs/**；desktop/scripts/stage-runtime-win.ps1；desktop/scripts/install-capability.py；desktop/scripts/install-capability-win.ps1；desktop/electron/capabilities.ts；desktop/electron/main.ts；desktop/electron/preload.ts；desktop/electron/sidecar.ts；desktop/src/App.tsx；desktop/src/services/ecorexApi.ts；desktop/src/vite-env.d.ts；desktop/src/styles/app.css；desktop/package.json；docs/ecorex-*.md。
背景：用户要求 Slack/Discord/Telegram/WeChat/DingTalk、语音、Playwright、Office/PDF 重型解析等不能撑大开箱安装包，但首次使用时必须可顺利安装，且 Agent 能主动判断要装什么。
实现摘要：新增 `capabilities.json` 能力包 manifest；新增跨平台 Python 安装器，写入结构化状态和日志；Windows 包装脚本调用同一安装器；Electron 主进程提供 list/install IPC；Settings 展示能力包状态、日志和安装按钮；发送前根据文本与附件自动识别缺失能力，在 chat 中展示安装确认卡片；stage 脚本改用 Python embeddable + 核心依赖；新增 `package:win:signed` 两阶段签名流程。
兼容影响：不改 agent core；内部 CowAgent 路由保持；可选依赖缺失时核心 web sidecar 仍可启动。
安全影响：能力包安装需要用户确认或管理员预置；失败状态和日志可审计；签名脚本仍不删除、不导入、不管理证书。
UI 影响：Settings 新增能力包区；chat 中新增“需要补充能力” human-in-the-loop 卡片。
验证方式：npm run typecheck；npm run package:win:signed；Office/PDF 能力包首次安装测试；未知能力包失败状态测试；轻量 runtime sidecar `/auth/check`；packaged unpacked app `/auth/check`；Authenticode 验证；能力包缺失模块探测。
验证结果：通过。核心 runtime 约 57.8MB；当时 Windows setup 约 112.3MB；setup 和 win-unpacked EcoreX.exe 签名 Valid。当前发布包大小见第 17 节。
已知风险：能力包下载仍依赖外网 PyPI，企业内网需要后续接镜像源/离线包缓存/管理员预置策略；macOS 安装器脚本虽为跨平台 Python，但 DMG 构建节点尚未验证。
回滚方式：恢复 stage 脚本为本机 Python copy；移除 capabilities IPC/UI；使用旧 `package:win` 产物。但会回到安装包体积过大的问题。
后续 TODO：Admin Web 的能力包策略已可保存；下一步需要桌面端拉取策略并应用 PyPI 镜像/离线包缓存；在真实普通用户机器执行安装器验收。
```

### 改动记录：macOS DMG Build Chain

```text
日期：2026-06-10
负责人：Codex
分支/提交：未提交
改动类型：Build / Security / Docs
改动范围：macOS runtime staging、DMG package scripts、签名/公证准备、CI artifact 工作流。
涉及文件：desktop/scripts/stage-runtime-mac.sh；desktop/scripts/validate-mac-artifacts.sh；desktop/package.json；desktop/electron-builder.yml；desktop/build/entitlements.mac.plist；.github/workflows/ecorex-desktop-release.yml；desktop/electron/capabilities.ts；desktop/electron/sidecar.ts；docs/ecorex-*.md。
背景：目标要求 macOS 直接输出 DMG。Windows 本地无法生成 Electron macOS DMG，且 macOS 已签名 app 不能把首次安装能力包写入 app bundle。
实现摘要：新增 macOS staging 脚本，按 arm64/x64 下载 python-build-standalone 并安装核心依赖；新增 macOS package scripts；新增 hardened runtime entitlements；移除 `identity: null` 以允许 CI 证书签名；新增手动 GitHub Actions release 工作流，可输出 arm64/x64 DMG artifact，可选 notarization/staple；macOS 能力包 target 切到用户数据目录并通过 PYTHONPATH 注入 sidecar。
兼容影响：Windows 能力包安装路径保持不变；macOS 首次安装不会修改 signed app bundle。
安全影响：macOS 签名公证链路可配置 Apple secrets；validate 脚本会检查 app bundle、runtime 文件、codesign、spctl、stapler。
验证方式：npm run typecheck；bash -n scripts/stage-runtime-mac.sh；bash -n scripts/validate-mac-artifacts.sh；Windows 能力包失败分支回归。
验证结果：本地可验证项通过。DMG 生成、codesign、spctl、notarization 仍需 macOS runner 实际执行。
已知风险：python-build-standalone 下载依赖 GitHub release，企业内网需要缓存；GitHub Actions 需要证书和 Apple 凭据才能产出可直接分发的已公证 DMG。
回滚方式：恢复 `desktop/package.json` mac 脚本为旧 `electron-builder --mac dmg`；恢复 `electron-builder.yml` 的 `identity: null`；移除 mac staging/workflow。但会回到无法开箱分发的状态。
后续 TODO：在 macOS runner 触发 workflow，下载 DMG，更新下载页 macOS 0.1.4 artifact/hash/size，并执行真实 macOS 安装验收。
```

## 9. Bug 修复模板

```text
日期：
问题标题：
用户现象：
影响范围：
复现步骤：
相关日志：
怀疑层级：Electron Main / React Renderer / Python Sidecar / Tool / MCP / Skill / Channel / Admin Web
根因：
修复文件：
测试方式：
真实用户验收：
回归范围：
是否需要热修：
```

## 10. 新功能记录模板

```text
日期：
功能名称：
业务目标：
用户角色：
入口位置：
涉及模块：
权限策略：
审计要求：
配置项：
API 变更：
UI 变更：
明暗模式影响：
测试用例：
真实用户验收：
灰度计划：
运维注意事项：
```

## 11. 运维排障入口

### 11.1 桌面端无法启动

排查顺序：

1. Electron main 日志。
2. Python sidecar 是否启动。
3. sidecar 端口是否冲突。
4. 本地配置是否损坏。
5. 企业策略是否阻止启动。

### 11.2 会话无法回复或疑似死循环

排查顺序：

1. 当前 goal 是否清晰。
2. 是否重复调用同一工具。
3. 模型 provider 是否异常。
4. 工具调用是否卡住。
5. 任务队列是否满。
6. human-in-the-loop 是否被隐藏或未触达用户。

### 11.3 文件无法预览

排查顺序：

1. 是否由用户点击触发预览。
2. 文件路径是否在允许 root 内。
3. `/api/file` 是否通过认证。
4. 文件类型是否支持。
5. Office/PDF 抽取依赖是否可用。
6. 文件是否超过大小限制。

### 11.4 Skill 无法安装或调用

排查顺序：

1. 安装来源是否被企业策略允许。
2. Skill namespace 是否兼容。
3. 依赖环境变量是否缺失。
4. Skill 是否已启用。
5. Agent 是否能发现该 Skill。
6. 调用日志是否进入审计。

### 11.5 MCP 无法加载或调用

排查顺序：

1. `<agent_workspace>/mcp.json` 是否存在且格式正确。
2. `mcpServers` 或 `mcp_servers` 字段是否正确。
3. stdio 命令是否在本机可执行。
4. SSE/HTTP 地址是否可访问。
5. 企业策略是否禁用该 server。
6. Agent 是否能发现 MCP tool。
7. MCP server stderr 日志。

### 11.6 Admin Web 无法追溯错误

排查顺序：

1. 桌面端是否上传错误事件。
2. 错误事件是否包含 user/device/version/session。
3. 日志采样策略是否过滤。
4. Admin Web 查询条件是否正确。
5. 事件链是否关联到会话和工具调用。

## 12. 安全与审计记录

需要审计的动作：

- 文件读取、写入、编辑、删除、移动、外发。
- shell/cmd 执行。
- Skill 安装、更新、启用、禁用、卸载。
- MCP server 添加、启动、禁用、调用。
- 外部 channel 发送消息、创建文档、上传文件。
- 管理员策略变更。
- 模型 provider key 变更。
- human-in-the-loop 用户选择。

审计字段建议：

| 字段 | 说明 |
| --- | --- |
| `eventId` | 唯一事件 ID |
| `timestamp` | 本地和服务器时间 |
| `actor` | 用户或系统 |
| `deviceId` | 设备 ID |
| `orgId` | 企业 ID |
| `goalId` | 当前 goal |
| `sessionId` | 会话 ID |
| `action` | 动作类型 |
| `target` | 文件、Skill、MCP、channel |
| `policyDecision` | allow/deny/confirm |
| `userChoice` | 用户选择 |
| `result` | success/failure |
| `errorCode` | 错误码 |

## 13. 发布检查清单

发布前必须检查：

- 应用名称、图标、窗口标题、安装包名称为 EcoreX。
- 聊天身份显示 EcoreX。
- 橙色品牌和明暗模式完整。
- 主界面双列，文件点击后预览。
- Settings Center 收纳 Skill/MCP/模型/通道/权限。
- Admin Web 可创建用户、看用量、查错误日志。
- 内部 `compatSource=cowagent` 仍可用。
- `cow` CLI 未被破坏。
- `ecorex` CLI alias 可用。
- 文件读写策略生效。
- 删除、shell、外部发送高风险动作有策略和确认。
- MCP server 可以加载、禁用、审计。
- Skill 可以安装、启用、发现、调用。
- 多 Agent 并发有 Goal Ledger 和任务租约。
- Windows 安装包签名。
- macOS notarization。
- Windows/macOS 普通用户安装后开箱即用，无需手动安装运行时依赖。
- 首次登录或组织绑定后能自动拉取企业策略和模板配置。
- 自动更新灰度通道可用。
- 诊断日志可导出。

## 14. 已知风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 全局替换 `cowagent` | Skill/MCP/channel 断链 | 使用 branding config 和 compatSource |
| Electron 资源占用 | 低配电脑卡顿 | 并发限制、懒加载、虚拟滚动 |
| MCP server 过多 | 启动慢、内存高 | 按需启动、空闲关闭、策略控制 |
| bash/cmd 权限过大 | 本地文件风险 | 默认确认、策略、审计 |
| 权限提示太多 | 用户烦躁后乱点允许 | Smart Ask、同类授权、清晰文案 |
| 权限提示太少 | 文件和外发风险 | 高风险动作强制确认 |
| 会话死循环 | 用量浪费、用户失控 | 循环检测、最大步骤、停止按钮 |
| 多 Agent 跑偏 | 修改无关模块 | TaskLease、DriftGuard、Goal Ledger |
| Office 预览不完整 | 用户无法检查原格式 | 第一阶段文本抽取，后续 PDF 转换 |
| 飞书模板改名 | 鉴权或路由失败 | 外显改名，内部 route 保持 |
| 工作区迁移 | 用户历史文件丢失 | 双路径兼容、显式迁移 |

## 15. 遗漏点跟踪

以下项目尚未进入详细设计，后续 goal 不能跳过：

| 遗漏点 | 为什么重要 | 跟进要求 |
| --- | --- | --- |
| SSO 与设备绑定 | 企业开箱即用依赖身份和设备可信 | Admin Web 阶段必须设计 |
| 企业代理和离线模式 | 很多办公网络限制外网 | Electron POC 后补网络策略 |
| 日志脱敏和留存 | 错误回溯不能泄露敏感内容 | 审计设计时同步完成 |
| prompt injection 防护 | 联网搜索和文件读取会引入外部指令 | 工具调用前加风险提示和边界 |
| MCP/Skill 供应链 | 外部能力可能带来高权限执行 | 来源签名、白名单、审计 |
| 更新回滚 | 企业发布失败影响面大 | 打包阶段必须验证 |
| 工作区迁移 | `~/cow` 历史数据不能丢 | 品牌兼容层阶段必须验证 |
| 可访问性 | 企业软件需要键盘和可读性基础 | UI Demo 阶段纳入验收 |
| 成本和配额 | 死循环和并发会放大费用 | Admin Web 阶段纳入用量策略 |
| 支持诊断包 | 线上问题需要快速定位 | Electron 阶段先留接口 |

## 16. 后续维护约定

- 新增功能优先补充到本文档的功能记录。
- 发现线上问题优先补充 Bug 修复模板。
- 架构方向变化新增 ADR，不覆盖旧 ADR。
- 涉及兼容字段变化必须更新兼容决策表。
- 涉及安全边界变化必须更新安全与审计记录。
- 每个 goal 结束时必须补充 Goal Ledger 的完成状态和下一步。

## 17. 2026-06-10 打包、签名与下载中心部署记录

```text
日期：2026-06-10
负责人：Codex
目标：按 EcoreX 桌面端方向完成 Windows 可下载签名包、部署双端下载页面与带账密保护的管理员页面。
涉及范围：desktop/**、deploy/ecorex-site/**、.gitignore、服务器 /srv/ecorex-agent-download、/opt/xhs-report/Caddyfile、/opt/xhs-report/docker-compose.yml。
未改范围：agent core、Skill/MCP 内部路由、CowAgent 兼容字段、客户端证书管理。
```

### 17.1 Windows 打包与签名

- 版本提升到 `0.1.4`，避免发布版本号低于服务器已有 `0.1.3` 页面。
- 复用 EcoreX 图标生成 `desktop/build/icon.png`、`desktop/build/icon.ico`、`desktop/build/icon.icns`，Windows 打包日志不再使用默认 Electron 图标。
- `electron-builder` Windows 配置使用 `signExecutable: false`，由外部 `signtool.exe` 签名脚本处理证书动作。
- 签名脚本固定为 SHA256 单签。曾验证 SHA1+SHA256 双签会导致 NSIS 安装器 `HashMismatch`，因此禁止再走双签。
- 已签名安装器：`desktop/release/EcoreX_0.1.4_x64-setup.exe`。
- 当前最终 SHA256：`7DE777D01CA84418276A48CE2F3D3F69664527AF2E9CEE3B49380BBC6611645C`。
- 当前最终体积：`117522648` bytes，约 `112.1MB`。
- 验证通过：`Get-AuthenticodeSignature` 为 `Valid`，安装器与 `win-unpacked/EcoreX.exe` 均为有效签名。
- 本机真实安装烟测通过：`npm run smoke:win:installed` 会将安装器静默安装到临时目录、启动已安装 `EcoreX.exe`、等待 sidecar `/auth/check` 返回成功，再卸载并清理临时目录。
- 当前包已内置核心 Python runtime 和 CowAgent 兼容运行时；Slack/Discord/Telegram/WeChat/DingTalk、语音、Playwright、Office/PDF、numpy/pandas 等重型依赖走能力包首次安装或管理员预置。
- 明确边界：签名脚本不会删除或修改客户端证书，只调用外部签名工具完成文件签名。

### 17.2 macOS DMG 状态

- 当前 Windows 环境执行 `npm run package:mac` 失败，失败原因为 `Build for macOS is supported only on macOS`。
- `Build macOS Apps` 插件可用于 macOS 产物验收、签名、公证、Gatekeeper 检查，但不能在 Windows 机器直接生成 Electron DMG。
- 已新增 macOS 构建链：
  - `desktop/scripts/stage-runtime-mac.sh`
  - `desktop/scripts/validate-mac-artifacts.sh`
  - `npm run package:mac:arm64`
  - `npm run package:mac:x64`
  - `.github/workflows/ecorex-desktop-release.yml`
- 有 `MAC_CERTIFICATE`、`MAC_CERTIFICATE_PASSWORD`、`APPLE_ID`、`APPLE_APP_SPECIFIC_PASSWORD`、`APPLE_TEAM_ID` secrets 时，工作流可进行签名、公证和 staple；无 secrets 时只能产出 unsigned DMG artifact，不满足企业开箱即用发布标准。
- 下载中心本轮继续保留服务器已有 macOS DMG：
  - `EcoreX-0.1.1-mac-arm64.dmg`
  - `EcoreX-0.1.1-mac-x64.dmg`
- 风险：macOS DMG 不是本轮 `0.1.4` 代码在 macOS 构建节点重新生成的产物。后续必须在 macOS runner 上生成 `0.1.4` DMG，并执行 `codesign`、`spctl`、notarization/staple 验收。

### 17.3 下载中心与管理员页面

- 本地发布包：`deploy/ecorex-site/`。
- 对外路径：`https://www.ecoreai.cn/ecorex-agent/`。
- 管理端路径：`https://www.ecoreai.cn/ecorex-agent/admin/`。
- 管理端使用 Caddy `basic_auth`，前端不包含明文密码。
- 管理员凭据明文仅存放在服务器非 Web 根目录：`/srv/ecorex-agent-download/admin-credentials-20260610.txt`，权限 `600`。
- 新 release：`/srv/ecorex-agent-download/releases/20260610-v0.1.4-admin-api-r6`。
- 上一版 release：`/srv/ecorex-agent-download/releases/20260610-v0.1.4-installed-smoke-r5`。
- Admin API 服务：Docker Compose service `ecorex-admin-api`，仅在容器网络内暴露 `18084`。
- Admin API 数据库：`/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3`，与下载站 release 目录隔离。
- `current` 软链已切换到上述 release，旧 release 保留可回滚。
- Caddy 修复：为容器增加只读挂载 `/srv/ecorex-agent-download:/srv/ecorex-agent-download:ro`，否则原 `/ecorex-agent` 路由在容器内无法访问文件，只会 404。
- Caddy / Compose 变更已备份：
  - `/opt/xhs-report/Caddyfile.bak.20260610_1315_ecorex_admin`
  - `/opt/xhs-report/Caddyfile.bak.20260610_1320_basic_auth`
  - `/opt/xhs-report/Caddyfile.bak.20260610_161056_ecorex_admin_api`
  - `/opt/xhs-report/docker-compose.yml.bak.20260610_1315_ecorex_admin`
  - `/opt/xhs-report/docker-compose.yml.bak.20260610_161056_ecorex_admin_api`
  - `/opt/xhs-report/docker-compose.yml.bak.20260610_161630_ecorex_admin_health`

### 17.4 部署验收

- `https://www.ecoreai.cn/ecorex-agent/`：HTTP 200。
- `https://www.ecoreai.cn/ecorex-agent/manifest.json`：HTTP 200，JSON 可解析。
- `https://www.ecoreai.cn/ecorex-agent/downloads/EcoreX_0.1.4_x64-setup.exe`：HTTP 200，`Content-Length=117522648`。
- 公网下载后 SHA256：`7DE777D01CA84418276A48CE2F3D3F69664527AF2E9CEE3B49380BBC6611645C`。
- macOS Apple Silicon DMG：HTTP 200，`Content-Length=194571881`。
- macOS Intel DMG：HTTP 200，`Content-Length=202268666`。
- 未登录访问 `https://www.ecoreai.cn/ecorex-agent/admin/`：HTTP 401。
- 使用管理员账号访问 `https://www.ecoreai.cn/ecorex-agent/admin/`：HTTP 200。
- 管理员页面已连接 `/ecorex-agent/admin/api/*` SQLite 后端，可创建用户、禁用/启用用户、更新角色、查看用量、写入/标记错误日志、保存能力包镜像源/离线缓存策略。
- `ecorex-admin-api` 容器健康检查为 `healthy`。
- 主站根路径仍保持原业务行为：HTTP 302 到 `/login`，未被 EcoreX 路由影响。

### 17.5 仍未完成的产品级验收

- Windows 安装器已在本机以临时目录执行真实静默安装、启动、sidecar 健康检查、卸载清理烟测；仍需在干净普通用户机器执行完整安装验收。
- macOS `0.1.4` DMG 尚未在 macOS 构建节点生成、签名、公证。
- Admin Web 已接入 SQLite 后端；尚未完成设备绑定、SSO、部门/模型维度用量、桌面端自动遥测上报和权限策略下发到客户端。
- Skill/MCP 安装、发现、启用、调用尚未完成真实桌面端闭环验收。

## 18. 2026-06-10 Admin API r6 留痕

```text
目标：把管理员页面从浏览器本地示例状态升级为有真实后端的数据管理面板，同时保持 EcoreX 下载路径隔离和原站点路由不受影响。
涉及范围：deploy/ecorex-admin-api/**、deploy/ecorex-site/admin/**、/opt/xhs-report/Caddyfile、/opt/xhs-report/docker-compose.yml、/srv/ecorex-agent-admin/data。
未改范围：agent core、Skill/MCP 内部路由、客户端证书、Windows 安装器内容。
```

新增能力：
- `deploy/ecorex-admin-api/ecorex_admin_api.py`：Python stdlib + SQLite，无第三方依赖。
- API 路径：`https://www.ecoreai.cn/ecorex-agent/admin/api/*`，与管理员页面共用 Basic Auth。
- 数据模型：users、usage_events、error_logs、capability_policy、capability_packs、audit_events。
- 管理员页面改为优先读取 API；API 不可用时仅回退到本地示例，避免页面空白。
- 用户管理：创建、禁用、启用、角色更新。
- 用量监控：按 category 聚合。
- 错误日志：写入事件、显示用户/设备/会话/tool 元信息、标记 error 已读。
- 能力包策略：保存 PyPI/内网镜像源、安装模式、离线缓存地址。

部署记录：
- 新 release：`/srv/ecorex-agent-download/releases/20260610-v0.1.4-admin-api-r6`。
- 当前公网下载包仍为 `EcoreX_0.1.4_x64-setup.exe`，`Content-Length=117522648`，SHA256 `7DE777D01CA84418276A48CE2F3D3F69664527AF2E9CEE3B49380BBC6611645C`。
- Compose 新增 service `ecorex-admin-api`，健康检查状态 `healthy`。
- Caddy 新增 `handle_path /ecorex-agent/admin/api*`，先于静态 admin 路由匹配。
- 主站根路径仍保持原业务行为：`https://www.ecoreai.cn/` 302 到 `/login`。

验证结果：
- `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py`：通过。
- `node --check deploy/ecorex-site/admin/admin.js`：通过。
- 未登录访问 `https://www.ecoreai.cn/ecorex-agent/admin/api/state`：HTTP 401。
- 登录后访问 `https://www.ecoreai.cn/ecorex-agent/admin/api/state`：HTTP 200，返回 SQLite 状态。
- 创建验收用户：通过。
- 禁用/启用验收用户：通过。
- 角色更新：通过。
- 写入错误事件并标记已读：通过。
- 保存能力包策略：通过。
- 管理员页面 `admin.js` 已包含 `const apiBase = "./api"` 和 `request("/state")`。

剩余风险：
- 管理端尚未接 SSO、设备绑定、部门维度、模型维度和配额。
- 桌面端尚未自动上报用量/错误/设备状态到 Admin API。
- Admin API 仍是最小后端，不等同于完整企业 IAM/审计平台。
## 19. 2026-06-10 Admin Model Policy r7 留痕

```text
目标：增加管理员模型连接权限点，使管理员能统一配置、修改、删除用户可用的模型 API Key 与 Base URL；用户安装桌面端后无需手动配置模型即可开始聊天。
涉及范围：deploy/ecorex-admin-api/**、deploy/ecorex-site/admin/**、desktop/electron/**、desktop/src/**、desktop/scripts/stage-runtime-win.ps1、desktop/scripts/stage-runtime-mac.sh、服务器 /ecorex-agent/client/* 路由、Windows 0.1.4 安装包。
不记录内容：真实 API Key、企业客户端通道密钥、服务器敏感地址、管理员密码。
```

新增能力：
- Admin API 新增 `model_credentials` 表，支持 provider、model、baseUrl、apiKey、scopeType、scopeValue、enabled。
- Admin Web 新增“模型连接策略”入口，可创建模型凭据、编辑 model/baseUrl/API Key、删除凭据；列表只显示 `apiKeyMask`，不回显完整密钥。
- 客户端新增 `/ecorex-agent/client/model-config`，桌面端用企业客户端密钥拉取当前可用模型配置。
- 客户端新增 `/ecorex-agent/client/events`，桌面端可上报 usage/error/warn/info；该接口只返回 `{ok:true}`，不返回管理员 state。
- Electron sidecar 启动前和每次发送消息前会刷新企业模型策略；检测到策略变化时重启 sidecar，使模型配置尽快生效。
- Windows/macOS runtime staging 支持写入 `enterprise-policy.json`，安装包只内置企业客户端通道密钥和策略 URL，不内置真实模型 API Key。
- 桌面端发送消息前若策略刷新导致 sidecar 重启，会短暂等待后再发起 `/message`，避免管理员刚改完配置时用户第一条消息失败。

发布记录：
- 新 release：`20260610-v0.1.4-model-policy-r7`。
- Windows 安装包：`EcoreX_0.1.4_x64-setup.exe`。
- Windows 安装包大小：`117524704` bytes，约 `112.1MB`。
- Windows 安装包 SHA256：`E2064B512B6038C06EB95AFD020BFF48F454221701D71247838834BF2DECC91F`。
- `current` release 已切换到 r7；旧 release 和 Caddy/compose 备份保留。

验证结果：
- `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py`：通过。
- `node --check deploy/ecorex-site/admin/admin.js`：通过。
- `npm run typecheck`：通过。
- `npm run package:win:signed`：通过，签名脚本未删除或修改证书。
- `npm run smoke:win:installed`：通过，静默安装、启动、sidecar `/auth/check`、卸载清理均成功。
- Windows 安装包 Authenticode：`Valid`。
- win-unpacked 内 `enterprise-policy.json` 存在并指向公开域名下的 client model/events 路由。
- 公网 manifest hash 已更新为 r7 hash。
- 公网下载完整文件 SHA256 与本地签名产物一致。
- `/ecorex-agent/client/model-config` 无客户端密钥返回 403。
- `/ecorex-agent/client/model-config` 带企业客户端密钥返回已配置模型策略。
- `/ecorex-agent/client/events` 带企业客户端密钥写入成功。
- `ecorex-admin-api` 与 Caddy 容器健康。

安全边界与风险：
- 当前 r7 使用“企业客户端共享密钥”保护 client 通道，适合内测和小范围试点；生产阶段应升级为设备级 enrollment token、短期会话令牌、SSO/设备绑定或 mTLS，避免安装包被逆向后复用客户端通道密钥。
- 模型 API Key 存在服务端 SQLite，Admin Web 只显示 mask；客户端只有在发送消息前按策略拉取运行时配置。
- 如果使用全局模型策略，所有持有有效企业客户端密钥的安装包都能拿到同一模型配置。生产建议按 user/device/org scope 下发，并把设备身份从当前 hostname/platform 升级为服务端签发的设备 ID。
- r7 未完成完整 IAM、部门、配额、设备禁用后的即时失效链路；后续 Admin Web 阶段必须补齐。

回滚方式：
- 将 `/srv/ecorex-agent-download/current` 软链切回 r6 release。
- 恢复对应 Caddyfile/docker-compose 备份。
- 重建 `ecorex-admin-api` 容器或移除 client route。
- 如需撤销测试模型策略，在 Admin Web 删除 `EcoreX default model` 或直接删除 `model_credentials` 对应记录。

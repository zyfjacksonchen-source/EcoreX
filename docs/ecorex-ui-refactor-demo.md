# EcoreX 桌面端 UI 重构方案与 Demo

<!-- Hallmark pre-emit critique: P5 H5 E4 S5 R4 V4 -->

本文档以当前产品判断为准：EcoreX 桌面端主界面采用 Codex 桌面端式布局、橙色品牌、明暗双模式、圆角组件、图标优先、hover 展示明细。Skill、MCP、模型、通道等配置统一收进设置入口；管理员能力单独做 Web 管理台，不塞进普通用户桌面主界面。

## 1. 产品体验原则

EcoreX 是企业用户每天使用的桌面 Agent，不做营销首页，打开后直接进入工作台。

设计原则：

- 品牌主色为橙色，表达广告、创意、执行力和温度。
- 可参考 Codex 的克制图标语言：几何、单色、低噪声、状态明确，但 EcoreX 图标必须自研，不直接复制商标或产品图形。
- 主界面采用 Codex 桌面端式双列：左侧为 session、项目上下文、运行状态侧栏；右侧为 chat 主工作区。
- 这里的“双列”不是两个业务大屏并排，也不是常驻三栏控制台。
- 文件预览不是常驻右栏，只有用户点击文件、工具结果或附件时才弹出预览。
- 技术细节默认收起，用用户能理解的话描述状态；hover 或点击后显示明细。
- 组件和颜色全部走 token，不在组件里硬编码颜色、圆角、阴影、动效时长。
- Skill、MCP、模型、通道、权限统一进入 Settings，不在主导航上堆满技术模块。
- 管理员页面独立为 Web 管理台，负责用户、用量、错误日志、策略、设备和审计。
- 明暗模式可自由切换，并跟随系统模式。

## 2. 视觉方向

整体气质：

- 橙色品牌锚点 + 暖白/深墨背景 + 少量蓝色信息态 + 绿色成功态 + 红色风险态。
- 圆角为主要形状语言，避免大面积硬方块。
- 主界面保持高信息密度，但每个区域有清晰呼吸感。
- 图标按钮优先，陌生图标必须有 tooltip。
- hover 展示“这是什么、当前状态、能做什么”，避免展示一串技术参数。
- 动态组件用于表达状态，不用于装饰炫技。

推荐动态组件：

- Streaming cursor：EcoreX 正在回复时显示柔和光标。
- Agent activity chip：显示“读取文件中”“等待确认”“搜索网页中”。
- Task pulse：长任务运行时的轻微呼吸状态点。
- Permission stepper：高风险动作以一步确认呈现。
- File preview drawer：点击文件后从右侧滑出，关闭后回到双列。
- Hover detail popover：会话、文件、工具、用量、错误日志 hover 展开明细。
- Toast stack：只显示用户需要处理的事件，普通后台状态进入底部状态条。

## 3. Token 体系

组件只引用 token。Tailwind class 中允许使用 `var(...)`，不允许直接写色值。下面是设计 token 的建议结构，正式实现时可放在 `desktop/renderer/src/styles/tokens.css`。

```css
:root {
  --font-sans: var(--font-app-sans);
  --font-mono: var(--font-app-mono);

  --radius-xs: 6px;
  --radius-sm: 10px;
  --radius-md: 14px;
  --radius-lg: 18px;
  --radius-xl: 24px;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;

  --motion-fast: 120ms;
  --motion-base: 180ms;
  --motion-slow: 260ms;
  --ease-standard: cubic-bezier(0.2, 0.8, 0.2, 1);
}

:root[data-theme="light"] {
  --color-bg: oklch(0.985 0.006 78);
  --color-surface: oklch(1 0 0);
  --color-surface-raised: oklch(0.995 0.004 78);
  --color-border: oklch(0.895 0.012 78);
  --color-text: oklch(0.215 0.025 50);
  --color-muted: oklch(0.52 0.025 65);
  --color-brand: oklch(0.69 0.18 48);
  --color-brand-strong: oklch(0.59 0.19 43);
  --color-brand-soft: oklch(0.94 0.05 58);
  --color-info: oklch(0.58 0.14 250);
  --color-success: oklch(0.58 0.13 150);
  --color-warning: oklch(0.74 0.16 78);
  --color-danger: oklch(0.6 0.19 28);
  --shadow-popover: 0 18px 48px oklch(0.32 0.03 50 / 0.16);
}

:root[data-theme="dark"] {
  --color-bg: oklch(0.16 0.018 55);
  --color-surface: oklch(0.205 0.018 55);
  --color-surface-raised: oklch(0.245 0.02 55);
  --color-border: oklch(0.34 0.025 58);
  --color-text: oklch(0.94 0.01 74);
  --color-muted: oklch(0.72 0.02 68);
  --color-brand: oklch(0.76 0.17 55);
  --color-brand-strong: oklch(0.82 0.16 58);
  --color-brand-soft: oklch(0.31 0.07 52);
  --color-info: oklch(0.74 0.12 245);
  --color-success: oklch(0.72 0.12 150);
  --color-warning: oklch(0.82 0.14 80);
  --color-danger: oklch(0.74 0.16 30);
  --shadow-popover: 0 18px 48px oklch(0 0 0 / 0.36);
}
```

明暗模式要求：

- 顶栏提供手动切换：Light / Dark / System。
- 首次启动跟随系统。
- 用户选择写入本地偏好。
- 企业管理员可设置默认模式，但普通用户可覆盖。
- 所有图表、日志、文件预览、代码块都必须有暗色样式。

## 4. 主界面结构

主界面采用 Codex 桌面端式布局，不再使用常驻多栏技术控制台。

布局解释：

- 左侧是稳定侧栏：session 列表、项目上下文、最近文件、活跃任务、运行状态。
- 右侧是主对话工作区：消息流、工具摘要、human-in-the-loop、composer。
- 预览不是常驻主列。点击文件、附件、diff 或工具结果后，才从右侧打开 preview drawer。
- Settings 不是主列。点击设置后以 dialog/sheet 进入配置中心。
- 管理员能力不在桌面主界面出现，只通过独立 Admin Web 使用。

```text
┌────────────────────────────────────────────────────────────────────┐
│ Top Bar: EcoreX  Workspace  Search  Theme  Settings  User          │
├─────────────────────────────┬──────────────────────────────────────┤
│ Codex-style Sidebar         │ Chat Workbench                       │
│ Sessions + Project Context  │ Main Conversation                    │
│                             │                                      │
│ - 当前项目                  │ - 对话流                             │
│ - 会话列表                  │ - human-in-the-loop 确认节点         │
│ - 最近文件                  │ - 工具执行摘要                       │
│ - 活跃 Agent                │ - 输入框和附件                       │
│ - 底部运行状态              │                                      │
└─────────────────────────────┴──────────────────────────────────────┘

点击文件或工具结果后：
┌─────────────────────────────┬─────────────────────┬────────────────┐
│ Codex-style Sidebar         │ Chat Workbench      │ Preview Drawer │
│ Sessions + Project Context  │                     │ File / Diff    │
└─────────────────────────────┴─────────────────────┴────────────────┘
```

桌面尺寸建议：

- 左列默认 340px，可在 300px 到 420px 间调整。
- 右侧 chat 自适应，最大正文宽度 860px，工具摘要可全宽。
- Preview drawer 默认 420px，可关闭，可拖拽到 560px。
- 小屏时左列变为抽屉，chat 保持主视图，preview 仍为点击后弹层。

左列内容：

- 项目卡片：项目名、组织、当前策略、用量状态。
- Session list：会话名、最后一步状态、是否等待用户确认。
- Active agents：正在运行的 Agent、排队任务、可停止按钮。
- Recent files：最近读取、生成、修改的文件。
- Status strip：sidecar、联网、MCP、队列、同步状态。

右列内容：

- Chat header：会话标题、当前身份 EcoreX、模型、运行状态。
- Chat flow：用户消息、EcoreX 回复、工具摘要、确认节点。
- Tool summary：默认用短句，例如“已读取 3 个文件”，点击后看技术明细。
- Composer：输入、附件、发送、停止、权限模式选择。

## 5. 设置入口

主界面只保留一个 Settings 入口，避免 Skill、MCP、模型、通道在主导航上分裂。

Settings 内部分区：

- General：语言、主题、启动项、通知。
- Models：模型、provider、key 状态、默认模型。
- Channels：飞书、企微、钉钉、Slack、Telegram 等通道。
- Skills：Skill 安装、启用、禁用、来源、版本。
- MCP：MCP server、工具暴露、transport、日志。
- Capabilities：飞书/IM、语音、Playwright、Office/PDF、模型 SDK 等能力包状态、首次安装、日志和管理员预置提示。
- Permissions：文件、联网、shell、外部发送、human-in-the-loop。
- Files：工作区、允许目录、预览策略、清理策略。
- Advanced：兼容 source、诊断包、开发者日志。

普通用户看到的是可用能力和权限选择；管理员策略禁止的项显示“由管理员管理”，不展示复杂技术堆砌。

## 6. 管理员 Web 管理台

管理员能力单独做网页，不放进桌面端主界面。

管理台核心模块：

- 用户创建与管理：邀请、禁用、角色、部门、设备绑定。
- 组织策略：模型、联网、文件、shell、Skill、MCP、外部通道。
- 用量监控：用户、部门、模型、任务、Skill/MCP 调用量。
- 错误日志回溯：按用户、设备、版本、会话、错误码检索。
- 审计日志：文件操作、权限确认、外部发送、管理员改动。
- 连接器模板：飞书等办公应用模板、bot 外显名称、兼容路由。
- 版本与灰度：Windows/macOS 版本、灰度圈、强制更新。

管理员页面视觉：

- 同样使用橙色品牌和明暗模式。
- 更偏数据表格和筛选器，不做聊天式界面。
- 错误日志 hover 展示摘要，点击进入完整调用链。
- 用量监控以真实数据为准，不写虚假指标。

## 7. 组件分层

### 7.1 桌面工作台组件

| 组件 | 职责 |
| --- | --- |
| `EcoreXAppShell` | 顶栏、双列布局、预览抽屉、底部状态 |
| `ProjectPanel` | 左侧项目信息、策略、用量概览 |
| `SessionList` | 会话列表、等待确认状态、运行状态 |
| `SessionRow` | 会话摘要，hover 显示最近步骤和文件 |
| `ActiveAgentList` | 多 Agent 运行状态、队列、停止 |
| `ChatWorkbench` | 右侧对话工作区 |
| `ChatMessage` | 用户、EcoreX、工具摘要消息 |
| `HumanGateCard` | human-in-the-loop 节点，清晰显示等待用户决策 |
| `PermissionChoice` | 一次允许、始终允许、拒绝、仅本次目录 |
| `ToolSummary` | 用户语言摘要，点击展开技术明细 |
| `FilePreviewDrawer` | 点击文件后显示预览 |
| `ThemeToggle` | Light / Dark / System |
| `SettingsCenter` | 所有设置入口 |

### 7.2 设置组件

| 组件 | 职责 |
| --- | --- |
| `SettingsDialog` | 设置总入口 |
| `SkillSettings` | 安装、启用、禁用、发现、调用测试 |
| `McpSettings` | server 管理、工具发现、连接测试 |
| `CapabilitySettings` | 能力包状态、首次安装、失败日志、管理员预置提示 |
| `PermissionSettings` | 文件、联网、shell、外发权限偏好 |
| `ChannelSettings` | 飞书等通道配置 |
| `ModelSettings` | provider 和模型 |
| `DiagnosticsPanel` | 日志和诊断包 |

### 7.3 管理台组件

| 组件 | 职责 |
| --- | --- |
| `AdminLayout` | Web 管理台整体布局 |
| `UserManagementTable` | 用户创建、禁用、角色 |
| `UsageMonitor` | 用量和成本监控 |
| `ErrorTraceTable` | 错误日志回溯 |
| `AuditLogExplorer` | 审计检索 |
| `PolicyEditor` | 企业策略 |
| `ConnectorTemplateManager` | 飞书等模板 |
| `ReleaseRingManager` | 版本和灰度 |

## 8. shadcn/Origin UI 使用方式

shadcn/ui：

- Button、Tooltip、Popover、Dialog、Sheet、Command、Tabs、ScrollArea、DropdownMenu、Select、Switch、Slider、Progress、Table、Badge、Toast、Resizable。
- 组件源码进入项目内，统一套 EcoreX token。
- 按钮、输入框、弹层、表格都要覆盖 default、hover、focus、active、disabled、loading、error、success 状态。

Origin UI：

- 参考组合模式，不直接形成远程依赖。
- 重点参考 settings、command menu、upload/dropzone、sidebar、status item、empty state。
- 改造为 EcoreX 组件命名和 token。

## 9. Demo TSX 骨架

正式开发建议放在 `desktop/renderer/src/screens/workbench/EcoreXWorkbenchDemo.tsx`。下面只表达结构。

```tsx
export function EcoreXWorkbenchDemo() {
  return (
    <div className="app-shell" data-theme="system">
      <TopBar
        brand="EcoreX"
        workspace="亦芯广告"
        actions={["search", "theme", "settings", "user"]}
      />

      <main className="workbench-grid">
        <ProjectPanel>
          <ProjectSummary name="亦芯广告增长项目" policy="企业策略已同步" />
          <SessionList />
          <ActiveAgentList />
          <RecentFiles />
          <RuntimeStatus />
        </ProjectPanel>

        <ChatWorkbench>
          <ChatHeader title="投放日报自动化" identity="EcoreX" />
          <ChatFlow>
            <UserMessage text="读取本地投放报告，结合今天的行业新闻，生成飞书日报草稿。" />
            <AssistantMessage>
              已读取 3 个文件，正在整理素材表现、预算消耗和行业信息。
              <ToolSummary label="查看执行明细" />
            </AssistantMessage>
            <HumanGateCard
              title="发送到飞书前确认"
              detail="EcoreX 将创建一份日报草稿，不会直接群发。"
              choices={["允许本次", "总是允许创建草稿", "拒绝"]}
            />
          </ChatFlow>
          <Composer permissionMode="ask-smart" />
        </ChatWorkbench>
      </main>

      <FilePreviewDrawer open={false} />
      <SettingsCenter />
    </div>
  );
}
```

CSS 结构只使用 token：

```css
.app-shell {
  min-height: 100vh;
  background: var(--color-bg);
  color: var(--color-text);
  font-family: var(--font-sans);
}

.workbench-grid {
  display: grid;
  grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
  gap: var(--space-3);
  padding: var(--space-3);
}

.panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
}

.brand-action {
  background: var(--color-brand);
  border-radius: var(--radius-md);
  transition: transform var(--motion-fast) var(--ease-standard);
}
```

## 10. 文件预览交互

文件预览必须由用户点击触发。

触发入口：

- 会话中的附件。
- 工具结果中的文件。
- 左侧 Recent files。
- 文件操作结果 toast。

预览行为：

- 图片、视频、音频直接预览。
- PDF 使用 PDF.js。
- Markdown、文本、代码使用阅读器或 Monaco。
- Office 第一阶段展示后端抽取文本和文件 metadata，提供“用系统应用打开”。
- 编辑类工具展示 diff。
- 删除、覆盖、外发必须经过权限确认。

## 11. UI Demo 验收

Demo 完成后应满足：

- 橙色为主品牌色，明暗模式均完整。
- 主界面符合 Codex 桌面端式布局：左侧 session/项目上下文侧栏，右侧 chat 主工作区。
- 不能做成两个等权业务列，也不能把技术检查器常驻成第三主列。
- Skill/MCP 等技术设置不在主导航散落，统一进入 Settings。
- 重型能力包不要求用户预先理解，EcoreX 在任务需要时自动提示安装，也可在 Settings 查看状态。
- 文件预览默认不占右栏，点击文件后才出现。
- hover 展示用户可理解的明细，不展示生硬技术词堆。
- 多用圆角，按钮、面板、抽屉、确认卡片形态统一。
- 所有颜色、圆角、阴影、动效都来自 token。
- human-in-the-loop 节点在对话中醒目但不打断阅读。
- 权限确认不过度打扰，但高风险动作有边界。
- 管理员页面作为独立 Web 管理台有清晰入口和完整信息架构。
# 2026-06-10 补充：Admin Web 模型策略入口

Admin Web 需要单独提供“模型连接策略”模块，用于企业管理员维护 provider、model、Base URL、API Key、作用范围和启用状态。桌面端普通用户不直接看到完整密钥；主界面只展示“企业模型策略已同步 / 未同步 / 同步失败”等用户可理解状态，详细凭据只在受保护的 Admin Web 中维护。

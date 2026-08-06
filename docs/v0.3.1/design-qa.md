# EcoreX v0.3.1 WebUI 视觉与交互验收

## 验收边界

- 产品：Windows/macOS 共用的 e-Mate WebUI；未新增独立桌面 UI。
- 主题：dark。
- 真实浏览器视口：320×568、1440×900；另以用户 647×251 截图作为首页裁切对照输入。
- 比较方法：用户参考图与同状态 WebUI 截图合并后逐项检查，不以单张截图代替验收。

## 对照结果

| 项目 | 对照证据 | 结果 |
| --- | --- | --- |
| 首页机器人、标题与 Composer | `artifacts/visual/compare-hero.png`、`webui-home-1440x900.png` | 机器人和标题完整；Composer 可见；窄视口可正常滚动，无横向溢出 |
| 项目选择器 | `artifacts/visual/compare-project-selector.png`、`webui-project-selector-1440x900.png` | 使用主题菜单，项目与“添加项目文件夹…”同时渲染 |
| 密码表单 | `artifacts/visual/compare-password.png`、`webui-settings-password-1440x900.png` | 当前密码独占首行，新密码与确认密码在宽屏并列，控件无挤压 |
| 普通任务列表 | `artifacts/visual/compare-task-list.png` | 普通任务行消息框图标已移除，列表无独立黑色底框 |
| 最近任务 | `artifacts/visual/compare-recent-tasks.png` | 标题为可聚焦按钮，点击后进入对应任务 |
| 能力分类 | `artifacts/visual/webui-capability-categories-1440x900.png` | 九个分类常显并使用现有 Lucide 图标；协作连接可筛选 |
| 归档任务 | `artifacts/visual/webui-archived-view-1440x900.png` | 可查看只读时间线、恢复；逻辑删除确认明确保留审计记录 |
| 下载页页眉与五机器人能力轮播 | `artifacts/visual/compare-download-topbar-source-implementation.png`、`download-page-carousel-1440x900-final.png`、`download-page-carousel-mobile-390x844-final.png` | 橙色 `e-Mate` 小字已删除；五个机器人保持同屏，首位机器人及能力说明可切换，桌面与移动端无横向溢出 |

## 真实交互复验

- 首页项目菜单可打开、选择项目并显示“添加项目文件夹…”。
- 最近任务点击后导航到目标 thread。
- 任务可归档、只读查看、恢复、再次归档并逻辑删除；删除后从目录消失。
- Composer 输入 `@` 后显示能力目录；选择“文档处理”后生成可移除的结构化能力标签。
- 能力中心“协作连接”展示正式连接器飞书、腾讯文档，以及后端目录中的钉钉、企业微信、微信、QQ、Telegram、Slack、Discord 等 Beta 外部 IM；未配置管理员凭据时按钮保持不可连接。
- Skill 导入默认只展示 ZIP 选择和“验证并安装”，Hub 发布位于高级操作。
- 设置中的默认/完全访问控件暴露为 switch；密码布局与对照结果一致。

## 响应式与可访问性

- 320×568：无横向溢出，导航、模型选择、Composer 和权限入口可见；axe 无 violation。
- 1440×900：无横向溢出，导航、模型选择、Composer 和权限入口可见；axe 4.12.1 为 0 violation、36 passes。
- axe 对首页标题的背景图渐变给出 2 个 `color-contrast` incomplete，非确定违规；合并对照图人工确认标题为高对比白/橙实色文字且无裁切。
- 修复趋势图容器禁止的 ARIA 属性后，1440×900 验收报告 `passed: true`。

## 最终结论

`final result: passed`

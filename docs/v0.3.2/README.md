# EcoreX v0.3.2 工程记录

## 基线与边界

- 基线标签：`v0.3.1-direct-candidate-94e454c`
- 基线提交：`94e454c5aed8c2c23717f7ab0b82e55920ae9d33`
- 开发分支：`codex/ecorex-v0.3.2`
- 产品版本：`0.3.2`
- 架构约束：React 只渲染 Runtime、Control Plane、Skill/MCP 与工具协议给出的公开投影；浏览器不推断运行终态，不接收原始工具参数、结果、本机路径或密钥。

聊天重构只覆盖流式消息与时间、工具调用、过程折叠、用户/助手消息、滚动追随、长会话虚拟化、动态交互投影和终态。输入区、权限、模型、附件、语音、`/`、`@` 以及右侧产物/文件变更面板不属于本轮重构；为修复全局契约、可访问性或验收阻断所做的最小兼容调整除外。

## 已实现

### 流式事实与时间协议

- `RuntimeTiming` 由服务端提供 `started_at`、`finished_at` 与 `duration_ms`；终态耗时不再由浏览器时间推算。
- item 与 interaction 增加可选 `created_seq`，跨类型呈现只按持久化事实序号排序。
- projection、replay 与实时事件都补齐同一组 timing/sequence 语义，历史记录由服务端派生兼容值，不增加数据库迁移。
- 帧内只合并相邻且属于同一 item 的 delta；tool、interaction 与 terminal 事实都是不可跨越的 flush 边界。
- 工具结果与公开状态在一次 reducer 更新中收敛；重复终态保持幂等。
- 运行中 turn 与 tool 共用单一 250ms 显示 ticker，并使用 bootstrap 服务端时间校准；ticker 不写入 reducer，终态立即切换到服务端持久化时长。

### Timeline、折叠与终态

- `buildTimelineTurns` 将 message、reasoning、tool、task、artifact 与 interaction 按 turn 和 `created_seq` 合并。
- `foldTurnProcess` 以最长正文与最后正文为稳定锚点，完成后折叠其余过程；pending interaction 和产物保持可见。
- 失败、取消或中断且没有正文时，首个过程段默认展开；archived reasoning atom 不再回到统一时间线。
- completed、failed、cancelled、interrupted、superseded 使用统一终态行；复制反馈只在真实剪贴板写入成功后显示。
- 旧的 120 条窗口分页已删除，改用 `react-virtuoso@4.18.11` 的动态高度 turn 虚拟化。

### 滚动与动态内容

- 距底 72px 内追随最新；向上滚轮在虚拟列表重新测量前立即解除追随。
- 初次定位、流式增高、图片/动态卡尺寸变化与点击“回到底部”都经过真实滚动容器校正。
- 点击回底使用 80ms 测量稳定窗口；用户离底后不因列表高度重新估算而被拉回。
- 120 turn 浏览器场景验证连续滚动、离底按钮与回底后的稳定追随。

### 全局工程优化

- 管理端继续使用签名、只读 gate projection；发布与 rollout 仍由 Control Plane 的显式操作控制，前端没有增加旁路写权限。
- Runtime、Skill/MCP、connector、tool schema、usage、artifact、retouch 与 image orchestration 保留现有后端权威边界；本轮修复了 strict projection、MCP OAuth mock contract、usage contract 与动态交互回执的端到端漂移。
- 图片预览缓存的 object URL 只在 React 消费者提交后回收；精准修图预取只在组件挂载时发生，继续受既有并发缓存约束。
- Python `requests` 从 runtime profile 移到 dev profile；runtime/cloud/dev/platform-stage/bootstrap 锁文件与摘要重新生成。
- macOS 临时目录、迁移 symlink 和本机路径校验改为真实平台行为，测试不再全局伪造 `os.name`。
- 所有版本源、候选/晋级 workflow、Windows/macOS 包名、烟测、下载站和部署模板统一为 `0.3.2`；历史迁移与 `docs/v0.3.1` 保持不可变。

## 精确 UI 参数

| 项目 | 参数 |
|---|---:|
| Timeline 轨道 / 助手正文 / 用户最大宽度 | `820 / 720 / 620px` |
| 轨道左右 / 顶部 / 底部内距 | `20 / 24 / 40px` |
| turn / block 间距 | `20 / 8px` |
| 头像 / 工具行 / 回底按钮 | `24 / 32 / 32px` |
| reasoning 最大高度 | `200px` |
| 用户正文最大高度 | `310px` |
| 跟随阈值 / 测量稳定窗口 | `72px / 80ms` |
| 颜色反馈 / block 入场 / 折叠 | `160 / 180 / 280ms` |
| 折叠 easing | `cubic-bezier(0.33, 1, 0.68, 1)` |
| 用户气泡 | `16px 16px 0 16px`，`8px 12px` 内距 |

`prefers-reduced-motion` 与 forced-colors 下保留可见焦点和文字状态，不依赖透明度或颜色单独传达终态。

## 证据

完整验收结果见 [acceptance.md](acceptance.md)。机器生成证据保存在 `.candidate/quality/`，其中包括 pytest JUnit、Playwright 日志、锁文件门禁、供应链报告与两份相同的 Web 字节契约。

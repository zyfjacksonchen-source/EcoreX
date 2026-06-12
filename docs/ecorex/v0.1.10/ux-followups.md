# EcoreX desktop UX follow-ups

记录时间：2026-06-12。读取时 `desktop/src/App.tsx` 与 `desktop/src/styles/app.css` 已有并行修改，本文只作为主 agent 集成清单，不要求覆盖正在进行的实现。

## 已阅读的原始 CowAgent WebUI 细节

- `channel/web/chat.html:226-241`：历史会话是可折叠 `session-panel`，移动端有 overlay，点击遮罩关闭。
- `channel/web/chat.html:256-259`：顶栏有历史入口，图标是 clock/history 语义。
- `channel/web/chat.html:317-401`：消息容器独立滚动；聊天区右下有 `scroll-to-bottom-btn`，悬浮在输入框上方，点击后强制恢复自动滚动并滚到底。
- `channel/web/chat.html:410-468`：输入区左侧是新对话、清上下文、附件；中间 textarea；右侧发送按钮会在流式回复中变成停止按钮。
- `channel/web/chat.html:1071-1095`：危险确认有统一 modal，而不是浏览器原生 `confirm`。
- `channel/web/static/js/console.js:1133-1141` 和 `3888-3901`：自动滚动用 80px 阈值。用户离开底部就暂停自动滚动，接近底部才恢复；悬浮按钮只在超过阈值时显示。
- `channel/web/static/js/console.js:1764-1855`：输入快捷键很细：IME composing 时不拦截；`Enter` 发送；`Shift+Enter` 或 `Ctrl+Enter` 插入换行；Slash 菜单用上下键、Tab、Enter、Escape；空输入或正在浏览历史时，上下键召回输入历史。
- `channel/web/static/js/console.js:1969-2152`：消息操作包含复制、编辑用户消息、重新生成回复。编辑会级联删除该轮及后续消息，再把原文回填到输入框；重新生成会删除旧用户消息和旧回复，再重新发送。
- `channel/web/static/js/console.js:2154-2244`：发送时先记录输入历史、移除欢迎屏、立即渲染用户消息和 loading，失败有 2 次递增延迟重试。
- `channel/web/static/js/console.js:2259-2640`：SSE 绑定 owner session。切到别的会话时后台流继续跑并缓冲，回到会话时可重放/重连，避免把外会话事件渲染到当前视图。
- `channel/web/static/js/console.js:3192-3287`：加载历史时第一页强制滚到底，并用 rAF + 多个 timeout 追上 markdown/highlight/image 后续撑高；加载更早历史时保持滚动位置不跳。
- `channel/web/static/js/console.js:3311-3424`：新建会话不会关闭其他会话的流；会打开历史面板，并乐观插入当前新会话。
- `channel/web/static/js/console.js:3431-3658`：会话面板 open 状态持久化；会话列表按 Today/Yesterday/Earlier 分组，50 条分页，滚到距离底部 60px 时加载下一页。
- `channel/web/static/js/console.js:3702-3786`：切换会话会重置历史分页并恢复该会话的发送按钮/流状态；删除当前会话时优先切到相邻会话，没有相邻会话才新建空会话。
- `channel/web/static/js/console.js:3788-3822`：确认弹窗支持点击遮罩取消、取消按钮、确认按钮，并带关闭动画。

## 当前桌面端观察到的状态

- `desktop/src/App.tsx:425,447,451,509-518,671-686,1410,1438-1447` 与 `desktop/src/styles/app.css:761-782` 已经出现“回到最新消息”的实现雏形：`showJumpLatest`、`messageListRef`、`autoScrollRef`、`updateJumpLatestState()`、`scrollToLatest()`、`.jump-latest-button`。
- 当前桌面端阈值是 96px；原 WebUI 是 80px。两者都合理，但为了交互一致建议统一成 80px 或抽到 `desktop/src/utils/chatUx.ts` 的 `CHAT_SCROLL_THRESHOLD_PX`。
- `desktop/src/App.tsx:238-275` 的 `mapSessions()` 只按 pinned 重排，不显式按 `last_active` 排序。不要用 `visibleSessions[0]` 代表“最新会话”，因为置顶、搜索、项目分组都会改变可见顺序。
- `desktop/src/services/ecorexApi.ts:253` 目前只取 `/api/sessions?page=1&page_size=40`；原 WebUI 是会话面板分页 50 条并滚动加载。桌面端的“一键回到最新会话”应基于当前快照里的最新真实会话，必要时点击前刷新一次 `loadRuntimeSnapshot()`。
- `desktop/src/App.tsx:767-785` 与 `828-830` 仍使用 `window.confirm` 删除会话/项目。WebUI 值得迁移统一确认 UI，尤其是删除会话这类高风险操作。
- `desktop/src/App.tsx:1100-1121` 只有 `Enter` 发送、`Cmd/Ctrl+Z` 撤销最后一轮；缺少 IME guard、`Ctrl+Enter` 插入换行、输入历史召回、Slash 菜单键盘语义。
- 当前消息渲染主干只展示 content/pending/attachments。另一个 worker 正在新增消息渲染组件时，可把复制、重新生成、编辑/删除整轮这类动作放到那个组件层，避免在 `App.tsx` 内继续膨胀。

## “一键回到最新会话”集成建议

建议把它做成会话层入口，不要复用聊天区的“回到最新消息”下箭头。

- DOM 位置：放在 `desktop/src/App.tsx` 的 `<aside className="session-sidebar">` 内，`<div className="sidebar-actions">` 里，紧跟“新对话”按钮之后、搜索框之前。这样它和会话列表导航同层级，且不会遮挡消息区或 composer。
- 备选 DOM 位置：如果主 agent 想减少顶部按钮密度，可放成 `.session-list` 顶部的 sticky 小按钮，但搜索过滤时容易和“返回搜索结果顶部”语义混淆。
- 图标：优先 `History` 或 `Clock3`（lucide-react）；如果强调“会话”而不是时间，可用 `MessagesSquare`。不要用当前聊天区的 `ArrowDownToLine`，否则会和“回到最新消息”混淆。
- 文案：`title`/`aria-label` 使用“回到最新会话”；如果已在最新会话，禁用态 title 可用“已经是最新会话”。
- 状态触发：先从 `runtimeSnapshot.sessions` 中计算真实最新会话，再与 `activeSessionId` 比较；没有真实会话、最新会话就是当前会话、或运行时未 ready 时禁用/隐藏。
- “最新”计算：按 `last_active` 最大值选，而不是按 `visibleSessions[0]`。如果 `last_active` 是秒级数字要转毫秒；如果是 ISO 字符串用 `Date.parse`；解析失败时退回接口顺序。已新增 `desktop/src/utils/chatUx.ts` 的 `findLatestSessionId()` 可直接复用。
- 搜索兼容：点击前清空 `searchQuery`，否则最新会话可能被过滤不可见但已切过去，用户会觉得侧栏没有 active row。
- 点击行为：先从当前 `runtimeSnapshot.sessions` 找 latest session id；必要时 await `loadRuntimeSnapshot()` 刷新；构造或查找对应 `SessionRow` 后调用现有 `selectSession(row)`。如果 latest 已是当前会话，只把消息滚到底即可，不切 session。
- 滚动行为：切到最新会话后保持 `autoScrollRef.current = true`，让 `selectSession()` 载入历史后自然滚到底；不要手动改 `messages` 或关闭任何 stream。
- 键盘可访问性：按钮使用原生 `<button type="button">`，保留可见 focus ring，提供 `aria-label` 和 `title`；Enter/Space 由浏览器处理。若加快捷键，建议只在焦点不在 textarea/input/contenteditable 时响应 `Alt+Shift+L`，避免和输入法、系统快捷键冲突。

伪代码：

```tsx
const latestSessionId = findLatestSessionId(runtimeSnapshot.sessions);
const canJumpLatestSession = Boolean(latestSessionId && latestSessionId !== activeSessionId);
const latestSessionRow = visibleSessions.find((row) => row.id === latestSessionId)
  ?? mapOneRuntimeSession(runtimeSnapshot.sessions.find((s) => sessionIdOf(s) === latestSessionId));

async function jumpToLatestSession() {
  const latestId = findLatestSessionId(runtimeSnapshot.sessions);
  if (!latestId) {
    scrollToLatest(true);
    return;
  }
  if (latestId === activeSessionId) {
    scrollToLatest(true);
    return;
  }
  setSearchQuery("");
  const row = findOrBuildSessionRow(latestId);
  if (row) await selectSession(row);
}
```

## “回到最新消息”微调建议

- 继续放在 `.chat-pane` 内、`.message-list` 之后、`.composer-zone` 之前；当前 `chat-pane` 已是 `position: relative`，按钮 absolute 定位是合适的。
- 按钮位置保持在 composer 上方 80-96px；当前 CSS `bottom: 92px` 可以保留，但文件预览 popover 也在 `bottom: 92px`，两者同时出现时需要避免重叠。
- 点击时设置 `autoScrollRef.current = true`，然后 `scrollTo({ top: scrollHeight, behavior: "smooth" })`，并隐藏按钮。
- 消息变化时如果 `autoScrollRef.current` 为 true 就自动滚到底；如果 false 只更新按钮显隐。不要在用户读旧消息时强制跳到底。
- 首次载入历史后建议仿 WebUI 增加 rAF + 120/350/700ms 的二次 pin 底逻辑，防止未来消息渲染组件加入 markdown、代码高亮、图片后撑高导致底部被截断。

## 其他值得迁移的 CowAgent 调教

- 输入框：加入 IME composing guard；保留 `Enter` 发送；补 `Ctrl+Enter` 插入换行；可选补单行空输入时上下键召回历史。
- 停止按钮：保留“点击发送按钮才取消，Enter 仍可发送 `/cancel` 文本”的语义，避免键盘发送和取消混在一起。
- 消息操作：复制回复原始 markdown；重新生成只在后端 seq 元数据可用时显示；删除用户消息时删除同一 turn 的 assistant 回复，避免上下文孤儿消息。
- 历史加载：加载更早消息时用 `prevScrollHeight` 保持 viewport；第一页加载后多次 pin 底。
- 会话切换：不要因为跳转最新会话而清理其他 session 的流。桌面端已有 `sessionRequestIds` 和 `streamCleanups.current[sessionId]`，应继续以 session id 为状态源。
- 删除会话：删除当前会话后优先选择相邻真实会话，不要直接新建空会话；否则用户从历史列表删除时上下文会突然丢失。
- 确认弹窗：用现有 `approval`/modal 样式替换 `window.confirm`，并支持 Escape/遮罩取消和明确危险按钮。

## 实现风险

- `visibleSessions[0]` 不等于最新会话：置顶、搜索、项目分组都会重排。
- `last_active` 类型不稳定：原 WebUI 使用秒级时间戳，桌面端 type 是 string；需要兼容 number 秒、number 毫秒、ISO 字符串和解析失败。
- 新建但未发送的本地草稿没有后端 session 记录，不应被“最新真实会话”入口选中；当前 active 草稿继续留在 `sessionUiState` 即可。
- 搜索过滤未清空时，切到了最新会话但侧栏 active row 不出现，视觉上像没生效。
- 点击最新会话时刷新 runtime snapshot 可能改变 row title/project 映射；刷新后仍要保留本地 `sessionTitles` 和 `sessionProjects` 覆盖。
- 多会话流式回复并行时，全局 `activeRequestId` 不应作为唯一状态源；以 `sessionRequestIds[sessionId]` 判断对应会话是否运行中。
- 新消息按钮和文件预览 popover 都在右下，`z-index` 和 `bottom` 要协调，避免按钮盖住预览操作。
- 若消息渲染组件开始渲染 markdown/图片/代码，高度会异步变化；只在 setState 后单次滚动到底会再次出现底部裁切。

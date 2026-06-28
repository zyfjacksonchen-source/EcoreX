# R23-20 会话身份、置顶/重命名与列表排序完整性整改计划

## 目标

修复并防止 EcoreX 在项目会话与通用会话之间出现内容串扰、会话消失、置顶后排序异常、重命名后自动置顶等问题。该切片只处理会话身份、列表归属、pin/rename 语义、刷新恢复和历史数据修复，不混入外部连接、CDP/OCR 或性能切片代码。

本切片必须遵守 v0.2.2/v0.2.3 核心方向：后端投影和持久状态是可信来源，前端不能靠本地缓存长期自推断；不能通过隐藏能力、减少消息、禁用投影或跳过恢复来掩盖问题。

## 用户可见症状

- 原本属于图生图 skill 的会话置顶在通用会话中，之后原会话疑似消失。
- 项目会话中显示了原图生图 skill 的内容，表现为项目/通用会话串内容。
- 重命名后会话会自动置顶。
- 置顶后列表存在 BUG，项目会话和通用会话都受影响。
- 期望会话列表排序参考 Codex：稳定、可预期，置顶和普通活动排序分离。
- 直接复制到聊天内的图片第一轮可用，第二轮继续追问时丢失原图上下文，模型无法找到上一轮图片。

## 已确认根因线索

1. 前端 `renameSession` 存在直接副作用：标题相同和标题变更两条路径都会执行 `setPinnedSessions(... true)`，导致“重命名后自动置顶”。证据：`desktop/src/App.tsx:4020` 到 `desktop/src/App.tsx:4035`。
2. 前端运行快照只拉第一页 `GET /api/sessions?page=1&page_size=40`。置顶旧会话如果不在第一页，且本地 UI state 被裁剪或冲突，就可能从列表消失。证据：`desktop/src/services/ecorexApi.ts:959` 到 `desktop/src/services/ecorexApi.ts:962`。
3. `mapSessions` 同时合并后端 sessions、本地 `sessionUiState`、activeRequests，并会为 active session 补幽灵行。该恢复机制需要保留，但必须增加 provenance 和不变量校验。证据：`desktop/src/App.tsx:1243` 到 `desktop/src/App.tsx:1379`。
4. project 归属优先级目前容易让本地旧缓存抢过 runtime/history 明确归属：`sessionProjectIdFromState` 优先读 `sessionProjects` 和 `sessionUiState.projectBinding`，runtime binding 只是 fallback。证据：`desktop/src/App.tsx:1221` 到 `desktop/src/App.tsx:1231`、`desktop/src/App.tsx:1266` 到 `desktop/src/App.tsx:1269`。
5. 后端 `ConversationStore` 能持久化 project 字段，但 `/api/sessions` 仍只按 `channel_type=web` 分页返回，没有显式 project/general scope 查询，也不返回 pinned 语义。证据：`agent/memory/conversation_store.py:1400` 到 `agent/memory/conversation_store.py:1474`、`channel/web/web_channel.py:11088` 到 `channel/web/web_channel.py:11101`。
6. SSE 接收侧当前主要校验 requestId，`shouldAcceptStreamItem` 没有强校验事件 payload 的 session_id。延迟尾包、恢复流或 request/session 映射异常时，存在写入错误会话的风险。证据：`desktop/src/App.tsx:5590` 到 `desktop/src/App.tsx:5601`、`desktop/src/App.tsx:5844` 到 `desktop/src/App.tsx:5875`。
7. `SessionRow.updatedAt` 同时承担展示文案和排序兜底，且可能是“运行中”“本地”“刚刚”等不可排序文本。R23-20 必须引入独立 `sortKeyMs`，展示字段不参与排序。
8. 全局 `allSessions` 先合成再按 `row.projectId` 分桶；如果 runtime-only project 没进入可渲染项目 catalog，row 可能既不在通用也没有项目组可显示。R23-20 必须保证 runtime/history 明确项目要么创建可渲染项目组，要么显式降级通用并记录原因。
9. 会话身份边界当前只有裸 `session_id`：sessions 表主键、messages 归属、load/history/list/runtime projection 都没有把 `scope/project_id` 纳入唯一性或查询边界。`append_messages(project_context=...)` 会覆盖同一 session 行的项目字段，而通用请求不会清空已有项目字段，因此 general session 可以被错误迁到 project bucket。
10. 会话“消失”还有后端可见性入口：`/api/sessions` 只返回 `channel_type="web"`，异常旧数据若是空 channel 会被过滤；项目分组渲染若只显示前 N 个项目，错误 project binding 会让会话从通用区消失但未必在可见项目组出现。
11. 正常发送链路会显式传递 `session_id` 和 `project_context_meta`，但恢复/重连/投影回放链路主要只信 `request_id`。`/stream` 和前端 EventSource 只带 `request_id`，`recoverRequestFromProjection` 不校验 projection 的 `session_id`，因此一旦本地 requestId 错挂到 B 会话，A request 的 replay/projection 可能写入 B。
12. RunLedger 的 `request_id -> session_id` 目前不是严格不可变 owner；RunEventLedger 也没有强制同一 request 的事件只能属于一个 session。这会让投影层无法在源头拒绝 mixed-session event。
13. 现有回归能证明项目/通用分组、fresh session 防迟到污染、RuntimeProjection 刷新恢复，但缺少“置顶 + 重命名 + reload + 旧 UI state/DB 脏数据”的组合 fixture。

## 不变量

- 一个 `session_id` 在同一个渲染周期只能出现一次，并且只能归属于一个 bucket：`general` 或 `project:<project_id>`。
- rename 只改 title 和 title lock，不改变 pinned、project binding、active session、last activity。
- pin 只由显式 pin/unpin 动作触发，不改变 project binding、消息内容、request/session 映射。
- activity 排序只由真实用户/助手消息、运行时 terminal/recovery 等会话活动驱动；metadata 编辑不能伪造成活动。
- requestId 必须唯一映射到一个 sessionId。stream、history、runtime projection 写入前必须校验 `(sessionId, requestId)`。
- RunLedger 中 `request_id -> owner_session_id/project_id` 一旦创建即不可变；RunEventLedger 不能接受空 session 或 mixed-session event。
- 后端存在的 session 是列表存在性的权威来源；前端 local row 只允许用于 draft/live/recovery，不允许长期替代后端 session。
- pinned、active、live、unread/recoverable session 不得因分页、裁剪或本地缓存 prune 被隐藏。
- history/runtime 明确 project_context 时优先于陈旧 localStorage；本地 binding 只能作为低可信兼容 fallback。
- 缺 session_id 的 runtime fallback row 只能展示诊断，不得持久化为 `runtime-${index}`。
- 会话身份契约必须显式包含 `{channel, scope, project_id?, session_id}`。同一个 `session_id` 跨 general/project 或跨 project 复用时，后端必须拒绝、显式 fork，或返回可审计冲突，不得静默覆盖 project 字段。

## Codex-like 排序语义

- 先按区域分桶：项目下只显示该项目会话，通用会话只显示无 project binding 的会话。
- 每个桶内先显示显式 pinned 会话，再显示普通会话。
- pinned 内按 `sortKeyMs DESC`，再按 `pinned_at DESC`，再按 `created_at DESC`，再按 `session_id ASC` 稳定排序。
- 普通会话按 `sortKeyMs DESC`，再按 `created_at DESC`，再按 `session_id ASC`。
- `sortKeyMs` 优先使用后端 `last_active`，其次本地未落库的新消息/运行中 request 时间；不得复用展示用 `updatedAt`。
- rename、title lock、测试连接、列表展开/折叠不更新 `last_activity_at`，也不自动 pin。
- 如果后端暂时没有 pinned_at，前端必须用明确的 pinned state 版本和 provenance，而不是用 activityAt 伪造 pinned 排序。
- 用户粘贴/上传的图片和文件必须作为附件 metadata 持久化；下一轮模型上下文必须能重新引用原始本地文件路径，但 UI 历史正文不能被 `[图片: path]` 这类内部上下文污染。

## 执行切片

### R23-20A：基线复现与证据冻结

- 构造无真实账号 fixture：2 条通用会话、2 条项目会话、1 条分页外旧置顶通用会话、1 条运行中项目会话。
- 复现并记录：rename 自动 pin、分页外 pinned 消失、本地 project binding 覆盖 runtime/history、延迟 history/projection 返回污染当前 session。
- 产物：`docs/v0.2.3/artifacts/session-cross-talk-baseline.json`、桌面/窄屏截图。

### R23-20B：后端会话列表投影契约

- 增加后端 session list projection 层，输出 canonical ownership：`scope`, `project`, `titleLocked`, `lastActivityAt`, `sourceRevision`, `provenance`。
- 明确 canonical identity：`{channel, scope, project_id?, session_id}`。`append_messages`、`load_messages`、`load_history_page`、`list_sessions`、runtime projection 都必须按同一 identity 约束工作。
- `append_messages(project_context=...)` 不能静默覆盖已存在 session 的 project；general -> project、project A -> project B 必须拒绝、显式 fork，或写入冲突事件。
- 支持查询：`scope=all|general|project`, `project_id`, `include_ids`, `include_pinned`，避免 pinned/live 会话因第一页分页缺失。
- 明确 pinned 状态来源；如果仍由 UI state 承载，必须通过后端 `/api/ui-state` 投影合并后返回，不让前端自行猜。
- 兼容旧 `channel_type` 空值数据：提供迁移或兼容查询策略，并把结果写入 dry-run evidence。
- PASS：后端同一 session 不会同时出现在 general 和 project 列表；跨 scope/project 复用被拒绝或 fork；分页外 pinned/live 会话可被补取。

Implementation note 2026-06-26: `ConversationStore.append_messages` now rejects project A -> project B and existing general -> project silent rebinding with `ConversationSessionOwnerConflict`; `SessionsHandler` projection rows now include `scope`, nested `project`, `lastActivityAt`, `sourceRevision`, and `provenance`. This is only a partial R23-20B implementation: `scope/project_id/include_ids/include_pinned` query parameters are still pending.

### R23-20C：前端会话归属纯函数收口

- 抽出 `resolveSessionOwnership` 和 `buildSessionRows`，统一后端 session、history、runtime、local UI state、activeRequests 的合并规则。
- 引入独立 `sortKeyMs`，禁止用 `updatedAt` 展示字段参与排序。
- 给每条 row 标记 provenance：`backend`, `runtime`, `history`, `localFallback`, `activeRequest`。
- local UI state 只能在后端/runtime/history 缺失时补位；不能覆盖明确 project_context。
- runtime-only project 必须进入可渲染项目 catalog，或明确降级通用并写入诊断原因。
- PASS：纯函数测试覆盖 stale local binding、runtime binding、history binding、runtime-only project、project deleted、general fallback、`updatedAt="运行中"` 不影响排序。

### R23-20D：pin/rename 语义拆分

- 移除 rename 自动 pin；标题相同只锁定 title，不改变 pinned。
- pin/unpin 独立动作，记录 pinned boolean 和 pinned_at；取消置顶不删除 title/project binding。
- metadata 更新不刷新 `last_active`；若需要 UI 反馈，用独立 `metadataUpdatedAt`。
- PASS：rename 后会话位置只因 title 展示变化，不因 pinned 或 activity 改变；显式 pin 才进入 pinned group。

Implementation note 2026-06-26: `desktop/src/App.tsx` direct rename/title-lock calls to `setPinnedSessions(... true)` were removed. This closes the direct rename->pin source path, but R23-20 remains incomplete until owner/scope, repair, privacy, and screenshot gates pass.

### R23-20E：request/session 强绑定与迟到包防护

- 建立 `requestSessionIndex: requestId -> sessionId`，来源优先级为 run ledger/runtime projection，其次当前发送返回。
- `shouldAcceptStreamItem` 必须校验 item 的 requestId 和 sessionId；缺 sessionId 的兼容事件只能在 requestSessionIndex 命中时接受。
- `/stream`、`/api/runtime-projection`、`/api/session-history-projection` 增加 expected session 参数；不匹配返回 `session_mismatch`，前端必须显示可恢复诊断而不是写消息。
- `RunLedger.create_run` 不得在 `ON CONFLICT` 时改写 owner session；`RunEventLedger.append_event` 必须校验 owner session，拒绝 mixed-session event。
- `RuntimeProjectionService.request_projection` 返回 owner session 并检测 mixed sessions；`session_projection` 只能纳入 owner session 等于当前 session 的 request。
- history/projection 刷新响应必须带发起时的 `(sessionId, requestId?)` guard；迟到响应不能覆盖当前 active session、pinned、title 或 project binding。
- PASS：运行中切换、rename、pin/unpin、reload、尾包到达均写回原 session；用 B session 订阅/投影 A request 必须 409/`session_mismatch` 且 UI 不写消息。

### R23-20F：历史 UI state/DB 审计与修复

- 新增 dry-run 审计脚本，检查 orphan `sessionProjects/sessionProjectBindings/sessionTitles/pinnedSessions`、重复 bucket、dangling project、runtime fallback id、无后端 session 的长期 local row。
- apply 前必须备份 `.ecorex/ui-state.json` 和 conversation DB；rollback 必须验证 SHA256。
- 证据只记录 hash、计数、脱敏 sessionIdHash、变更类型，不记录 raw prompt、消息正文、完整本地路径、邮箱、token。
- repair 默认只修 UI state 和 session 元数据，不删除 `messages` 正文；任何硬删候选只能进入 dry-run report，需单独人工确认。
- PASS：dry-run/apply/rollback 都有 artifact；修复后 general/project 集合 disjoint，orphan pin/title/binding 为 0。

### R23-20F-S：安全与隐私门禁

- `session_mismatch`、owner conflict、repair report、RunEventLedger 诊断只能记录 `sessionIdHash`、`requestIdHash`、`projectIdHash`、basename/hash、计数和错误码。
- 对外 artifact 的 hash 使用本地 salt/HMAC，不能用裸 SHA256 暴露可关联标识。
- 不允许记录 raw prompt、assistant content、tool result、完整本地路径、邮箱、token、cookie、credential、未脱敏 project path。
- 不允许记录 raw `session_id`、`request_id`、`project_id`、`projectPath`、`memoryPath`、`dreamsPath`、file name、attachment preview URL、full localStorage dump、full DB row、message extras。
- owner mismatch 不能把另一个会话的内容回显给当前会话；UI 只显示通用可恢复错误和 request hash。
- 修复脚本必须支持 `--dry-run` 默认模式；`--apply` 前创建备份，`--rollback` 验证备份 hash 后恢复。
- 修复脚本必须拒绝在运行中 request 存在时 apply，避免改写活跃会话归属。
- DB 修改必须事务化，执行前后运行 `PRAGMA integrity_check`，并设置 row-count 上限和断言。
- v0.2.2 旧数据中的 `channel_type=''`、orphan UI state、未知 project 必须 quarantine/兼容，不得清空或硬迁移。
- ChatService/WebChannel/diagnostics 日志不得写 raw prompt、完整 path、credential；新增 logging gate 断言。
- 所有 R23-20 artifacts 必须通过 denylist 扫描，命中 raw prompt/path/token/session/request/project 即 release blocker。
- PASS：Security/Audit reviewer 确认 owner gate、privacy gate、repair gate、legacy gate、logging gate、UI write gate 全部可执行并无 P0/P1/P2 泄漏或破坏性迁移。

### R23-20G：Codex-like 列表 UX 与可见性

- 项目和通用会话列表分桶渲染，禁止跨桶去重后丢失。
- pinned、active、live、recoverable 会话必须保持可见；若后端分页不足，前端触发补拉或 include_ids 补取。
- 未知项目、超过首屏项目数量、折叠项目下的 active/running 会话仍必须可达；不能因为项目列表裁剪而“消失”。
- 行操作按钮 hover/窄屏不遮挡标题，不触发误点。
- PASS：60+ 会话、分页外 pinned、窄屏、项目折叠/展开、搜索过滤均稳定。

Implementation note 2026-06-26: `desktop/src/App.tsx` now adds explicit `sortKeyMs` to `SessionRow`. Sorting first separates pinned from unpinned, then sorts each group by `sortKeyMs DESC`, then `createdAt DESC`, then `session_id ASC`. `updatedAt` remains display-only and no longer participates in sorting fallback.

Final UX decision 2026-06-27: EcoreX keeps Codex-like ordering inside each visible bucket, while preserving the existing project/general section model. This means pinned sessions sort above unpinned sessions within the same project bucket or the general bucket; project sessions do not jump above the project header into a global pinned rail. `smoke-web-session-cross-talk-browser.py` now covers both general-bucket and project-bucket pinned ordering, so R23-20G no longer has a global-pin caveat.

### R23-20I：历史附件上下文恢复

- `append_messages` 已把用户附件写入 message extras；下一轮 `load_messages` 必须从 extras 中恢复受控附件引用，例如 `[历史图片: local_path]`。
- 恢复只用于 LLM 上下文，`load_history_page` 仍返回干净用户正文和附件 metadata，避免 UI 气泡出现内部路径提示。
- 只恢复有限数量附件引用，保持上下文成本可控。
- PASS：直接复制/上传图片后，下一轮历史上下文包含原图路径；UI 历史正文仍是用户原文；附件卡片仍从 extras 渲染。

Implementation note 2026-06-26: `ConversationStore.load_messages` now restores limited `[历史图片/历史文件: path]` references from persisted user-message extras for model context only. `load_history_page` remains UI-clean and returns attachment metadata separately. This does not migrate older sessions that never persisted attachment extras.

### R23-20H：多 agent 审查与发布门禁

- Runtime/Backend reviewer：会话投影契约、DB migration、UI state repair 通过。
- Frontend/UX reviewer：rename/pin 语义、排序、窄屏交互、无文本溢出通过。
- Harness/Test reviewer：browser smoke、race fixture、repair dry-run/rollback、release blocker 通过。
- Security/Audit reviewer：artifact 脱敏、无 raw prompt/path/token、无破坏性迁移通过。
- Release/Regression reviewer：v0.2.2 会话隔离、Run Center 隐藏、刷新恢复、Feishu readiness 不回退。

## 必跑测试与产物

- `tests/test_ecorex_session_identity_sorting.py`
- `tests/test_ecorex_session_state_repair.py`
- `tests/test_ecorex_request_session_owner_contract.py`
- `tests/test_ecorex_session_privacy_gates.py`
- `tests/test_ecorex_session_legacy_repair_compat.py`
- `scripts/smoke-web-session-cross-talk-browser.py`
- `scripts/smoke-web-session-cross-talk-refresh-replay.py`
- `scripts/audit-ecorex-session-state.py --dry-run`
- `scripts/scan-session-artifacts-privacy.py docs/v0.2.3/artifacts/session-cross-talk-*.json`
- `npm --prefix desktop run typecheck`
- Browser artifacts:
  - `docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.json`
  - `docs/v0.2.3/artifacts/session-cross-talk-browser-smoke.png`
  - `docs/v0.2.3/artifacts/session-cross-talk-browser-smoke-narrow.png`
  - `docs/v0.2.3/artifacts/session-cross-talk-refresh-replay.json`
  - `docs/v0.2.3/artifacts/session-cross-talk-repair-dry-run.json`

## 当前审查状态

- Frontend/UX: PLAN-PASS by Leibniz. 确认 rename 自动 pin、分页/本地缓存/activeRequests 多源合并、project binding 优先级和 stream session guard 风险。
- Harness/Test/Release: PLAN-PASS by Aristotle. 确认现有覆盖缺少 pin/rename/reload 组合 fixture，并要求新增无账号 browser smoke、repair dry-run/rollback 和 release-blocking gate。
- Sorting/Product Semantics: PLAN-PASS by Arendt. 确认 rename 自动 pin 是确定路径、`updatedAt` 展示字段混入排序兜底、全局合成再分桶需要独立 scope 排序和 runtime-only project 可见性保障。
- Runtime/Backend: PLAN-PASS by Hume. 确认裸 `session_id` 身份边界、`append_messages` project_context 静默覆盖、`/api/sessions` channel_type/page 可见性和 hard delete/archive 语义缺口。
- RuntimeProjection/History: PLAN-PASS by Avicenna. 确认普通发送链路显式绑定 session，但 `/stream`、runtime projection recovery、RunEventLedger mixed-session 校验和前端 projection 写入 guard 是高风险缺口。
- Security/Audit: CONDITIONAL by Gibbs. 要求 owner gate、privacy denylist scan、repair/apply/rollback gate、legacy v0.2.2 fixture、logging gate 和 UI write gate 可执行后才能转 PLAN-PASS。

R23-20 已完成实现审查：owner/scope、repair/privacy、legacy fixture、browser cross-talk、refresh replay、bucket-scoped Codex-like sorting、历史附件上下文恢复和 release regression evidence 均已收敛。最终 v0.2.3 发布仍由 R23-17 总门禁统一确认。

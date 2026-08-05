# e-Mate WebUI v0.3.0 当前开发记录

更新时间：2026-08-04

## Goal

以线上 v0.2.9.2 为唯一升级基线，完成 e-Mate WebUI/Runtime v0.3.0。主界面以任务 `019fc2b8-92c9-7691-a972-90df97061774` 与只读目录 `C:\e-Mate-正式版` 为视觉事实，保留现有聊天框、设置、管理端、下载页和原位更新兼容。

## 续接规则与固定决定

- 每完成一个可验证切片更新本记录；不得覆盖用户已有未提交改动，尤其 `usage_panel_service.py` 的 12,000 行上限。
- 不再使用 1.0.18 双版本；产品和 Runtime 本轮统一目标 0.3.0。
- 前端所有可见 EcoreX 文案改为 e-Mate；机器产品 ID、API、数据目录、安装包文件名和旧下载 URL 保持兼容。
- 默认模型为 `ecorex-chat → gpt-5.6-luna`、`reasoning_effort=high`、272,000 Token 压缩阈值；GPT-5.5 只留历史反序列化兼容。
- 普通部署默认 full_access/never；用户主动切回受限模式后永久保留。认证、租户隔离、密钥脱敏、更新签名和目标路径完整性不放开。
- Usage、账户页、首页和 Audit 共用唯一投影；Asia/Shanghai、request_id 去重、Gateway 终态优先，不补造历史 Token。
- Product Design 复用目标 DOM/CSS/资产，不新增 UI 依赖；交付前必须用应用内 Browser 做同视口视觉对比和 design QA。
- Cow Skill Hub 固定上游 commit `0c214c3a61f66f8c122111c23270bd146241001b`，保留 MIT NOTICE；线上不依赖 CowAgent。

## 切片状态

- [x] S01 自动生图意图与连续图片上下文。
- [x] S02 Luna high 默认模型与 GPT-5.5 活动目录清理。
- [x] S03 Skill 默认启用、可关闭、跨会话持久化和轻量提示。
- [x] S04 full_access 一次迁移与 Runtime 权限事实统一。
- [x] S05 Usage/Audit 规范投影与对账元数据。
- [x] S06 自助改密后端、全会话撤销与设置入口。
- [x] S07 e-Mate 2.1.47 五机器人主界面、真实首页数据、创意/能力中心和设置改密入口。
- [x] S08 消息流、连续执行、真实 Task List 与 AICSS 状态动效。
- [x] S09 0.2.9.2 → 0.3.0 单版本在线更新与发布链（本地实现与门禁完成，未部署）。
- [ ] S10 全量回归、Browser 视觉 QA、Windows/macOS 升级验证与交付。
- [ ] S11 能力中心与 Skill Hub；权威后端、签名安装意图、基础 UI、内置/工作区迁移和视觉 QA 已完成；上游逐包种子缺真实不可变包，生产 OS Runner 缺签名隔离协议，均保持 fail-closed。

## 本轮已完成事实

- 生图路由唯一来源为 `intent_routing.py`；普通生成、改图和连续指令无需 `@imagegen`，否定/只分析优先。
- 模型、权限、Usage/Audit、自助改密后端均已落地并通过定向回归。改密只接受当前 bearer principal，成功后递增凭据版本并撤销全部 access/refresh/device session。
- 连续执行根因已修复一部分：工具后空响应只允许一次不重放工具的强制文字收口；二次为空失败而非伪装完成；不同 Feishu 目标的成功批处理不再消耗收敛失败预算；Feishu 已确认失败进入可恢复 failed 投影。
- 生图 Shell 旁路探测器的静态方法误用了 `self`，会在特定 Python/Node 图像命令判断时抛出 `NameError`；已改为调用共享类级意图判断并增加无实例回归，避免稳定性修复本身打断连续执行。
- ExtensionService 成为安装/状态权威：`extension_generation` 复用 append-only event seq；支持卸载 tombstone；公开投影含 provenance/readiness/requirements/tags/configure/uninstall。
- 全局 Skill Hub 已有不可变 slug/version/摘要、HMAC 作者别名、三类搜索、认证上传自动发布、CAS 校验下载；生产 migrate 创建表/CAS，check/serve 只验证，复用托管账号和现有密钥材料。
- Runtime 已通过托管会话 HTTPS 同源代理 Hub 列表/下载/上传；安装做摘要绑定、CAS 摄取、健康检查和默认启用；同版本关闭后直接重新启用，不重复下载。
- 能力中心基础 UI 已接真实“发现/已安装/自建”、搜索/分类、安装/开关/卸载与全站上传；前端类型检查通过，尚需按目标截图做完整视觉校准。
- Agent 已强制 `skill_search → skill_read → skill_run` durable 顺序；每步复核最新状态/版本，关闭、卸载、升级后旧授权返回 `skill_state_changed`；说明型 Skill 返回 `skill_not_executable`，未接任意 Shell。
- Cow 上游锁、53 个候选、11 项排除和完整 MIT NOTICE 已记录；因公开目录没有逐包不可变摘要，`seed_packages_locked=false`，不得伪装为已完成种子摄取。
- ExtensionService 启动迁移现统一摄取打包内置与工作区 Skill 到 CAS：固定 5 个别名和 11 个排除 slug；相同旧内置副本折叠，摘要不同或带 `.ecorex-custom-override` 的版本优先；旧启停状态/缺省启用、`feishu-lark` 缺省关闭、卸载 tombstone 均持久化，且不删除源目录。迁移脚本只作为不可执行 CAS 内容保存，普通本地上传仍维持原脚本拒绝边界。
- 首页五机器人透明资产已精确复用并保留现有 Composer；首页 Token 改接独立 `/api/v1/usage` 账号投影，仍复用和会话 Usage 相同的 Runtime/Gateway 合并规则。Thread 目录新增权威 `last_turn_status`，并在 Turn 状态变化时更新活动时间，首页完成数、等待数和成功率不再从 metadata 或“无活动 Turn”猜测。
- 设置页已接真实自助改密代理；校验当前密码、新密码和确认密码，成功后清除本机会话并回到登录页。
- AICSS 消息状态已绑定真实 Runtime 事件：公开 reasoning 执行中展开、完成后折叠；联网搜索和生图使用真实工具状态；流式 Markdown 删除额外 48ms 定时缓冲和重复 deferred 渲染，仅保留事件层一层 `requestAnimationFrame` 合并。
- Skill Hub 严格响应校验已移入按需加载的独立边界模块；低频能力中心不再把完整卡片/版本校验器塞进首屏。生产 bundle gate 恢复通过，initial gzip JS 为 149.87 KiB，仍低于既有 150 KiB 门禁。
- 新增真实 `task_list` 核心工具、`task_list.updated` 事件和持久化 Item：2–8 项、最多一个 `in_progress`、幂等写入；失败/取消的 Turn 在界面标为“任务已中断”，未完成项不伪造完成。工具完成结果在同一 Turn lease 下写入清单，回放可重建相同状态。
- 在线更新旧客户端桥已恢复：`ecorex.__version__` 是 0.3.0 唯一版本源；兼容 manifest 只接受与版本一致的双平台 verified receipt，并重新读取包字节核验文件名、大小和 SHA256，全部通过后才原子替换独立 `legacy-pointer/manifest.json`。Nginx/Caddy 将旧 `/ecorex-agent/manifest.json` 固定映射到该指针，签名 v1 合约 schema 名未改，未使用或发布历史 0.3.0 哈希。
- Skill Hub 上游种子门禁改为 fail-closed：53 个候选仍只有真实 slug/version 元数据、没有逐包 ZIP 摘要，故证据明确为 `blocked`（53 pending/0 verified）。未来只有每项补齐真实 package file/size/SHA256/CAS SHA256、离线重读 ZIP 并经临时 e-Mate CAS 规范化完全一致后才能将 `seed_packages_locked` 置为 true；门禁不联网、不写用户目录，固定 11 项排除清单必须与迁移权威一致。
- 在线更新兼容复核完成：Python/Runtime/Desktop/CLI 版本锚点均为 0.3.0，活动代码无 1.0.18 或 `core_version`；旧 `/api/version`、`/api/update-check` 保留且把新更新权威指向 `/api/v1/update`，签名检查/下载/激活仍走现有 UpdateService；健康失败、启动崩溃和确认前回滚的既有恢复链保持通过。公开旧 manifest 只能从独立 `legacy-pointer` 路由读取，生成器验证双平台包后才原子替换。生产上传/readback、真实两平台包和线上原子切换仍是发布环境门禁，本地未部署、未声称完成。
- Skill Hub 发现 API 已补真实 `tag` 与原始 `source` 精确筛选，继续复用同一 Registry 最新版本查询和游标；详情接口返回同一 slug 的全部不可变版本，并按 SemVer 倒序，不复制包或启停状态。Control Plane、托管会话 HTTPS transport 与本机 Runtime 代理均透传筛选/详情，Runtime 对详情中的每个版本叠加同一 ExtensionService 本机安装/readiness 事实。
- Skill 脚本清单采用包内精确 `skill-runtime.json`，声明入口、Python/Node/受信 Shell、环境变量、域名、外部命令和 effects；配置值只进入现有 OS 凭据保险库，投影/事件只返回键名。未接入真实 OS 隔离 Runner 前 readiness 明确返回 `unsupported/controlled_runner_unavailable`，不以宿主 Shell 冒充完成。
- 受控 Skill Runner 已落最小可信适配合同：只传冻结 extension/revision/CAS 摘要、已索引 Python/Node 入口、精确声明的 env/effects 与 generation fence，不存在命令字符串或宿主 Shell 通道；`skill-runtime.json` v1 尚无参数 schema，因此当前严格拒绝所有非空参数。执行前、Runner 内启动前/等待中及返回后都要求状态 fence，Runner 未绑定时继续 fail-closed。现有已签名 AppContainer/Seatbelt 权威仍不能直接复用：其启动协议固定为签名 Pack Python artifact，Windows read-root 不含 Skill CAS，且两端都没有域名级网络白名单或 Node 身份，因此没有伪造生产 Runner。
- Skill Hub 根因审计补齐了三处权威边界：Hub 的排除清单与迁移共用同一常量并阻止重新发布/安装；`docx/xlsx/pptx/pdf/lark-cli` 安装和卡片状态优先绑定现有 `skill.*` 原生提供方，不再下载第二份别名实现；下载后的 CAS metadata version 必须与请求版本一致。作者昵称若为空、等于账号 ID 或形似邮箱统一投影为 `e-Mate 用户`。Skill contribution 现绑定 Extension 状态 revision，配置、关闭、卸载或重新启用后旧搜索/read/run 授权继续返回 `skill_state_changed`，只有新快照恢复授权。
- Control Plane 已增加真实全局安装意图与 append-only 安装日志：短时 HMAC 意图精确绑定当前 principal 的不可枚举 account ref、slug、version、CAS digest 和 expiry；消费在事务内单次 `created → claimed`，重复、跨账号和过期均拒绝；独立 completion receipt 只允许同账号把 claimed 收口为 `installed/failed`，日志有数据库 update/delete 禁止触发器且不写邮箱、账号 ID、token 或 receipt。Runtime 的托管会话代理可自行创建意图或消费 `install_intent`，逐项复核身份后才下载/启用，成功/失败如实回写全局日志；未注册 `emate://` OS 协议。
- 产品、桌面包和 Runtime 版本源已统一为 0.3.0；签名发布指针的反回滚序列从硬编码 1.x 改为有界完整 SemVer，并保持 0.3.0 序列高于历史 1.0.17 序列。恢复 `/api/version` 和只读 `/api/update-check` 兼容投影，后者明确以 `/api/v1/update` 为权威且不再返回 `core_version`。
- 前端、设置、管理端和下载页完成外显品牌审计，用户可见标题/错误/探针统一为 e-Mate；协议头、localStorage key、模型本地 ID、API/下载路径和安装包兼容标识保持不变。

## 验证记录

- S01 图像意图：106 passed。
- S02 模型目录/激活/Gateway/Runtime：44 + 18 + 1 + 1 passed。
- S03 Skill 治理：7 + 1 passed。
- S04 权限账本/迁移/bridge：17 + 1 + 1 passed。
- 生图旁路/权限/连续执行聚焦回归：5 passed；修改 Python 全量 Ruff 通过。
- S05 Usage/Audit：11 passed。
- S06 密码与设备会话：34 passed。
- S08 批处理与失败投影：4 + 1 passed；工具后空响应等定向回归通过。
- S11 Extension 平台：51 passed, 1 skipped；严格披露：31 + 35 passed。
- S11 受控 Runner 合同与 Extension 组合回归：54 passed, 1 skipped；生产 OS 隔离适配仍按上述精确缺口保持关闭。
- S11 Hub 权威/别名/作者脱敏/状态 revision 围栏定向回归：8 passed；契约生成 `--check`、上游锁 53 项与 ruff 通过。
- S11 安装意图/全局日志/Runtime HTTPS 消费代理组合：29 passed, 1 skipped；ruff 与 py_compile 通过。
- S11 Hub Registry/Runtime/HTTPS bridge：4 passed；Control Plane 生产组合：19 passed。
- S11 内置/工作区 Skill 迁移：1 passed；Extension 平台组合回归：23 passed, 1 skipped；真实打包目录 14 个内置 Skill 全部摄取并启用。
- Cow 上游锁：`validated 53 seed candidates at 0c214c3`。
- Web Runtime contract `--check` 与 TypeScript `tsc --noEmit` 通过。
- S08 Task List/Runtime 组合：22 passed；前端 reducer 19 passed；消息流/合约/密度定向门禁 17 passed；生产 Web 构建通过（32 个内容寻址资产，入口 gzip 14.76 KiB，初始 JS gzip 149.38 KiB）。
- S09 旧客户端兼容 manifest 原子发布门禁：1 passed；篡改任一包时拒绝并保持原 manifest 字节不变。
- S11 Skill Hub 逐包种子 fail-closed 门禁：1 passed；当前发布证据为 53 pending、0 verified，发布保持阻断而非伪造摘要。
- S09 在线更新复核：版本/legacy manifest/API/public pointer 14 passed；UpdateService 与失败回滚 36 passed；更新只读/恢复执行边界 7 passed。
- S11 Skill Hub tag/source/版本历史后端与 transport：6 passed；WebUI 详情/下载/安装由并行切片接线，本切片未覆盖其文件。
- S09 版本源/签名指针/发布器：18 passed；旧 `/api/version`、`/api/update-check` 与兼容 manifest：2 passed。
- S07 e-Mate 2.1.47 首页：明暗主题 1440×900 同视口对照完成；Creative/Capability 主路径可交互；前端全套 206/206 passed，生产构建与 bundle gate 通过。
- S20 WebUI Hub 接线及 deferred 边界后前端全套 208/208 passed；TypeScript 与生产构建通过（33 个内容寻址资产，initial gzip JS 149.87 KiB）。
- 首页统一 Usage/Thread 终态：9 passed；RuntimeClient 49 passed；TypeScript/生成契约检查通过。
- 外显品牌：Python 定向 38 passed；品牌 Node 合约与 TypeScript 检查通过；1 项 wheel 打包测试因当前 `.venv` 无 pip 被排除。
- 更大组合中仅观察到本机缺少可选 numpy 的 OCR 失败；最终门禁仍需完整执行。

## 下一步

1. 补齐 Cow 上游逐包不可变 ZIP/摘要后再完成 CAS 种子锁；没有真实包时保持 fail-closed。
2. 若现有已签名隔离原语能完整承载 Skill CAS、Python/Node 与声明式网络边界，则接入生产 Runner；否则保持明确不可用，不退回通用 Shell。
3. 收口 0.2.9.2 → 0.3.0 在线更新兼容审计、发布清单与外显品牌门禁。
4. 执行本机全量回归；Windows/macOS 真实安装包升级、Luna high 连通和线上发布留待对应环境与凭据。

## 外部条件

真实 Luna high 连通、线上发布凭据、Windows/macOS 实包升级必须在对应环境完成；本机先完成所有可运行门禁并记录精确阻塞。

## v0.3.0 发布与升级完成矩阵（2026-08-04 最终审计）

| 门禁 | 当前状态 | 完成证据 / 阻断 |
|---|---|---|
| 单一产品版本 | 本地通过 | Python、Runtime、Desktop、package-lock、CLI 均为 `0.3.0`。 |
| 禁止双版本 | 本地通过 | 活动产品/发布代码无 `1.0.18`，`/api/version` 不返回 `core_version`。 |
| 旧客户端发现 | 本地通过 | `/api/version`、`/api/update-check` 保留；公开 `manifest.json` 独立 no-store 指针。 |
| 新客户端签名更新 | 本地通过 | `/api/v1/update` 的 check/download/activate 继续使用签名 feed、摘要验证和现有 UpdateService。 |
| 稳定版本反回滚序列 | 本地通过 | 根修 Bootstrap 与候选供应链仍写死 `1.x` 的问题；公开指针、候选构建、供应链校验和 Bootstrap 现在共用唯一 `stable_release_sequence`，`0.3.0 → 30001`。 |
| manifest 最后发布 | 本地通过 | 双平台 verified receipt 与实际包字节全部匹配后才 `fsync + os.replace` 原子替换；篡改保持旧指针。 |
| 下载 URL 兼容 | 配置通过、上线待验证 | Nginx/Caddy 保留 `/ecorex-agent/downloads/*`；本次补齐 cloud-sidecar 活动路由。URL、包名、机器 ID、数据目录未改。 |
| 管理端兼容 | 配置通过、上线待验证 | `/ecorex-agent/admin/` 仍代理现有 Control Plane；未改管理工作流。 |
| 数据保留 | 结构测试通过、实包待验证 | slot 更新不覆盖外置用户数据，旧入口清理测试保留用户文件；仍需真实工作区/会话/连接器/设置升级前后对账。 |
| 健康失败回滚 | 本地通过、实机待验证 | 健康失败、启动崩溃、确认前回滚与 known-good 保留测试通过；仍需真实包断网/坏哈希/坏健康检查演练。 |
| Windows x64 v0.3.0 包 | 阻断 | 当前 `release-artifacts` 与站点 downloads 均无本轮构建包/verified receipt。 |
| macOS universal v0.3.0 包 | 阻断 | 当前无本轮构建包；本环境也不是 macOS 实机。 |
| 签名与上传 readback | 阻断 | 当前没有本轮签名材料、生产发布凭据、远端 hash/size readback。 |
| 公开 manifest 0.3.0 | 未执行（符合本轮要求） | 本轮明确不部署；线上仍为 0.2.9.2，只有最后原子发布后才能改变。 |
| 0.2.9.2 → 0.3.0 两平台升级 | 阻断 | 缺当前实包、Windows/macOS 基线安装环境和生产等价网络；未形成当前源码对应的升级证据。 |

`docs/v0.3.0/artifacts` 中 2026-07 的历史 0.3.0 报告对应旧构建摘要，不能替代当前源码的发布产物、上传 readback 或升级验收，不得用于解除上述阻断。

本轮最终回归：Bootstrap/本地安装/legacy manifest/版本源 `24 passed, 1 skipped`；旧 `/api/version`、`/api/update-check` 与签名 authority 契约 `1 passed`；公开指针、候选包 minimum-stable 与 Windows helper 定向回归 `3 passed`；候选供应链与公开指针完整文件 `29 passed, 1 failed`，唯一失败是本机 `.venv` 的 `annotated-types` 与锁文件不一致（`installed_dependency_lock_mismatch`），与版本序列和更新代码无关，但发布前必须用锁定构建环境重跑为全绿。相关 Python/测试文件 Ruff 通过。

---

## 历史开发记录（完整保留）

以下内容为 2026-08-04 当前基线重做之前的 v0.3.0 开发记录；与上方固定决定冲突时，以上方当前决定为准。

# EcoreX v0.3.0 Development Log

## 2026-07-07

- Scope: WebUI-only v0.3.0 hardening.
- Target branch: `codex/ecorex-v0.3.0-hardening`.
- Target version: `0.3.0`.
- Release title: `EcoreX 0.3.0 生产级任务控制与在线更新稳定性版本`.
- Safety snapshot created before implementation:
  - `git stash push -u -m "v0.3.0 pre-implementation dirty tree snapshot"`
  - Snapshot retained as the latest stash entry at creation time.
  - Snapshot includes tracked dirty files and untracked files.
- Workspace cleanup completed:
  - Removed regenerable cache directories only: `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.parcel-cache`, `.vite`.
  - Verified delete targets were inside the workspace before removal.
  - Did not delete unknown release/docs artifacts or user-visible generated assets.

## Standing Rules

- Every implementation slice must update this log.
- Do not silently revert pre-existing dirty worktree changes.
- WebUI validation must include user-path evidence, not only source-level checks.
- Any release/update pipeline failure must stop the v0.3.0 release chain and avoid producing half-trusted artifacts.

## Slice Status

- S00 workspace safety and cleanup: complete.
- S01 version metadata and release copy: complete.
- S02 active turn control: complete.
- S03 WebUI stability fixes: complete.
- S04 release/update chain hardening: complete.
- S05 admin management productization: complete.
- S06 external connector discovery and preservation: complete.
- S07 real-user acceptance evidence: complete for packaged/local smoke; credentialed and destructive-environment residuals are tracked in the acceptance checklist.
- S10 retouch/infinite canvas completion: complete.

## S06 External Connector Discovery And Preservation

- Product scope correction:
  - Removed frontend-only planned connector placeholders from the external connections quick panel.
  - `/api/external-connections` now returns `ecorex.external-connectors.implemented.v1` and only catalogs real implemented connectors.
  - Tencent Meeting, Tencent Survey, QQ Mail, Lexiang, ima, and finance connectors are documented as researched/not-yet-implemented rather than shown as connectable UI.
- Stable discovery:
  - Added `ToolManager.ensure_mcp_configured_loaded()` so workspace `mcp.json` connectors can be started/refreshed by runtime discovery without relying on a one-time settings page state.
  - Wired the ensure path into runtime capabilities, extension registry, skill service, Web channel tool snapshots, agent initialization, streaming turns, and MCP hot reload.
  - Tencent Docs attachments now trigger a bounded MCP ready check before agent execution.
- Online update preservation:
  - `manifest.json` now requires connector health checks for WebUI online updates.
  - Windows and macOS package installers capture external connector snapshots before update and after new runtime start.
  - Update activation fails with `rollback` when previously connected/callable connectors disappear after update.
  - `update-state.json` exposes redacted `externalConnections` health details for WebUI and acceptance evidence.
- Research:
  - Added `docs/v0.3.0/external-connectors-real-connectivity.md`.
  - Official API/OAuth routes and original CowAgent/current EcoreX implementation status are recorded there.
- Verification:
  - `python -m py_compile agent/tools/tool_manager.py agent/runtime_capabilities.py agent/extensions/registry.py agent/skills/service.py bridge/agent_initializer.py bridge/agent_bridge.py agent/protocol/agent_stream.py channel/web/web_channel.py`

## S05 Admin Management Productization

- Admin release backend:
  - Added release-index validation to staged/current release validation.
  - Promotion now blocks when v0.3.0 release-index is missing, not ready, mismatched with manifest artifacts, missing smoke pass evidence, or missing required artifact signatures.
  - Release entries now expose release-index status, rollout, kill-switch, rollback, state machine, background update policy, risks, and next actions.
  - Admin release notifications no longer mutate immutable v0.3.0 manifest/release-index packages; they write admin release notice and local update-state instead.
- Admin page:
  - Release panel now shows release-index trust status, risk count, rollout percent, kill-switch, rollback health check, online update state machine, and release risks.
  - Current stable and staged candidates show release-index status and expandable validation failures.
  - Disabled `通知用户` state is enforced in the click handler, not only visually.
- Verification:
  - `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py channel/web/web_channel.py`
  - `node -e "const fs=require('fs'); new Function(fs.readFileSync('deploy/ecorex-site/admin/admin.js','utf8')); console.log('admin js syntax ok')"`

## S04 Release And Online Update Chain Hardening

- Online update state machine:
  - Runtime update state now accepts `available`, `downloading`, `verified`, `staged`, `deferred`, `installed`, `activated`, `failed`, and `rollback`.
  - Release notices use `available` instead of the old ambiguous `ready`.
  - Installed/activated states require health check pass before the WebUI offers immediate switch.
  - Runtime update banner is visible again and distinguishes download, deferred, failed, rollback, retry, switch, and log-view states.
- Installer state output:
  - Windows and macOS package installers now write `available -> downloading -> verified -> staged -> installed/activated` into `update-state.json`.
  - Background updates still defer when active requests exist or active request state is unavailable.
  - Manual installs finish as `activated`; background installs finish as `installed` so existing tabs can soft-refresh after health check.
- Release orchestrator:
  - Added `scripts/release-ecorex-webui-orchestrator.ps1`.
  - Orchestrator gates version alignment, typecheck, renderer build, WebUI package build, web service package build, artifact hash, signature presence, manifest trust, smoke evidence, and atomic `release-index.json` promotion.
  - `desktop/package.json` exposes `webui:release`.
- Verification:
  - `npm run typecheck`
  - `python -m py_compile channel/web/web_channel.py deploy/ecorex-admin-api/ecorex_admin_api.py agent/tools/browser/browser_service.py agent/protocol/image_job_service.py agent/protocol/runtime_projection.py agent/tools/imagegen/imagegen.py`
  - PowerShell scriptblock parsing for `scripts/release-ecorex-webui-orchestrator.ps1` and `scripts/prepare-ecorex-webui-local-release.ps1`
  - Node JSON parsing for `deploy/ecorex-site/manifest.json`, `deploy/ecorex-site/release-index.json`, and `desktop/package.json`
  - `npm run build:renderer`
- Packaging and release-index status:
  - `scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` completed for Windows and macOS WebUI packages.
  - `scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` promoted `deploy/ecorex-site/release-index.json`.
  - `deploy/ecorex-site/manifest.json` download source order is now domestic GitHub mirror first, origin CDN fallback second.
  - Windows package: size `551244443`, SHA256 `8E2FEA63006B9518FF05BE4FD1D4967A9B4C981DC44B5DF31901FFD925CEAC5D`.
  - macOS package: size `652419412`, SHA256 `A05AF02233E7B1F4498CAEC1410EC75CDE8C719C1E912955199210889BD3BE52`.
  - Release-index status: `ready`, smoke status: `pass`.

## S07 Real-User Acceptance Evidence

- Completed source/build checks:
  - `tests/test_v030_webui_hardening.py`
  - `tests/test_v029_webui_followups.py::test_webui_online_update_uses_ready_dialog_instead_of_confusing_banner`
  - `tests/test_ecorex_web_parallel_backend.py::TestProjectSessionSourceContracts::test_react_project_session_composer_autosize_and_general_isolation`
  - Result: `8 passed`.
- Completed build checks:
  - `npm run typecheck`
  - `npm run build:renderer`
- Completed user-path evidence:
  - Packaged WebUI runtime smoke passed against `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`.
  - User online update local smoke passed through the public install script, manifest download, package hash check, background install, runtime `/api/version`, runtime `/api/update-check`, external connector preservation policy, and no-browser background update check.
  - Release-package CDP smoke passed through a real headless Chrome/Edge browser against the packaged WebUI: session open, image artifact shelf, precise retouch entry, right-side current artifacts, two-image selection, text target, rectangle selection, lasso selection, uploaded reference image, marker attachment, and composer draft with imagegen-only constraints.
  - Evidence:
    - `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`
    - `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`
    - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.json`
    - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.png`
- Remaining real-user evidence:
  - Credentialed connector quick panel in a packaged browser path, live CDP reconnect, provider-level imagegen output, and rollback-on-failed-runtime still need environment-backed validation before public promotion.

## S08 Office-Agent UX And ImageGen Routing Hardening

- Entry:
  - User reported that both single-character poster fixes and the `精准修图` entry still routed through repeated local `bash`/Python image processing.
  - User also reported first-message new-session pending state gaps, occasional output truncation after streaming, persistent circular tool icons, code artifacts showing in an office-agent chat, and top bar text-chip noise.
- Frontend runtime state:
  - Added local `PendingPreflightTurn` tracking so the first message's assistant pending state remains stable before the backend returns a server `request_id`.
  - New same-session sends now supersede an unaccepted local preflight turn with an explicit "已被新消息替换" state instead of leaving an empty/vanishing thinking state.
  - Active-turn controls now appear whenever a local pending assistant exists, not only after a server request id exists.
  - Done-event content merge now preserves already-streamed longer content unless a true `final_text` is provided, reducing accidental final-packet truncation.
  - History merge keeps a locally richer visible answer when the refreshed history projection is shorter.
- Image generation and retouch routing:
  - `精准修图` drafts now explicitly require `imagegen` / image-editing capability and forbid bash, Python, PIL, OpenCV, ImageMagick, SVG/canvas, or coordinate scripts as the semantic edit path.
  - `agent_stream` imagegen intent detection now includes `精准修图`, `局部修图`, `精修标注`, `标注图`, `箭头尖端`, single-character text-fix phrases, and poster/image edit phrases.
  - Tool schema selection continues to expose only `imagegen` for imagegen intent when available; if unavailable, it exposes diagnostic/enablement tools only (`host_diagnostics`, `optional_abilities`, `agent_capability`, `ecorex_cli`), not bash.
  - Execution layer hard-blocks bash/shell/terminal during semantic image retouch tasks unless a prior `imagegen` call succeeded and the shell command is deterministic post-processing such as copy, rename, zip, checksum, or reveal.
- Office-agent artifact and UI polish:
  - Chat artifact shelf filters implementation/code files such as `.py`, `.js`, `.ts`, `.sh`, `.ps1`, native code, logs, lockfiles, etc.
  - Markdown artifacts remain visible and keep a local-open action pinned.
  - Tool-step icons no longer render a persistent circular chip; state is conveyed by the plain icon color.
  - Project add-folder uses a plain icon style.
  - Header runtime/account state now uses icon-only status controls with tooltip/aria labels.
  - Sidebar "通用会话" copy is simplified to "会话".
- Verification:
  - `python -m py_compile agent/protocol/agent_stream.py channel/web/web_channel.py`
  - `npm run typecheck`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `15 passed`
  - Focused existing regressions:
    - `tests/test_ecorex_web_parallel_backend.py::TestProjectSessionSourceContracts::test_react_project_session_composer_autosize_and_general_isolation`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_prioritizes_imagegen_for_multi_image_requests`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_uses_diagnostics_when_imagegen_is_missing_not_bash`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_does_not_restore_recent_bash_for_imagegen_intent`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_does_not_restore_other_tools_for_imagegen_intent`
    - Result: `5 passed`
  - `npm run build:renderer` passed with the existing large chunk warning.

## S03 WebUI Stability Fixes

- CDP/browser:
  - Added stale-browser detection for structured action return values such as `{"error": "Target closed"}`.
  - CDP mode now reconnects once when an action returns a stale connection result, matching the existing exception-based recovery path.
- Image generation:
  - Added stable artifact ordering by `(task_index, artifact_index)` in image job state, runtime projection, and frontend rendering.
  - Image job artifacts now carry `task_index`, `artifact_index`, and `task_id`.
  - Imagegen output names now encode task and artifact ordinals (`tXX/iXX`) instead of relying on timestamp/order alone.
  - Incremental image batch events carry `artifact_index`.
- Composer/scroll stability:
  - Reworked composer autosize to avoid unconditional `height = auto`.
  - Stop and pause state updates preserve message-list scroll position when the user is not already pinned to bottom.
  - New active-turn transient phases are cleaned like other preflight phases.
- Session list:
  - Project and general session lists show the first six rows by default and expose `查看更多(N)`.
  - Search mode still shows all matching rows.
- Share:
  - Share thumbnails are bounded to smaller JPEG data URLs.
  - Share payloads are capped by byte budget and message/artifact count.
  - Recent messages and recent images are kept first; older media degrades to metadata if needed.
  - `payload too large` retries once with media stripped.
- Visual boundary:
  - Composer zone now has a completed top divider and rounded top corners.
- Verification so far:
  - `python -m py_compile agent/tools/browser/browser_service.py agent/protocol/image_job_service.py agent/tools/imagegen/imagegen.py agent/protocol/runtime_projection.py channel/web/web_channel.py`

## S09 Package Evidence Refresh

- Entry:
  - After S08 imagegen-routing and office-agent UX changes, v0.3.0 WebUI packages had to be rebuilt and package smoke evidence had to point to the rebuilt artifacts, not the previous 17:00 package hash.
- Rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551244443`
    - SHA256: `8E2FEA63006B9518FF05BE4FD1D4967A9B4C981DC44B5DF31901FFD925CEAC5D`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652419412`
    - SHA256: `A05AF02233E7B1F4498CAEC1410EC75CDE8C719C1E912955199210889BD3BE52`
- Added repeatable package runtime smoke:
  - `scripts/smoke-v030-webui-package-runtime.ps1`
  - The script extracts the release zip into a bounded `tmp/` smoke directory, writes an isolated local `config.json`, starts package-internal `runtime/app.py`, verifies `/api/version` and `/app/`, records package hash/size/runtime manifest/release metadata, then stops the process.
  - Recursive cleanup is guarded so it can only operate under the repo `tmp/` directory.
- Verification:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-smoke -ExpectedVersion 0.3.0 -Port 9929`
  - Result: pass.
  - Evidence: `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`.

## S09B User Online Update Smoke

- Entry:
  - User requested a final check that the user-side online update path is actually effective.
  - User also requested the first download source to use a domestic GitHub mirror for speed.
- Changes:
  - `manifest.json` download source order is `ghproxy.net` GitHub release mirror first and `https://dl.ecoremedia.net/ecorex-agent` fallback second.
  - Public Windows install script now honors `ECOREX_DOWNLOAD_DISABLE_PARALLEL=1` and skips adaptive Range download for localhost smoke sources.
  - Local online-update smoke uses a deterministic `fileName` mirror so Python's local static server does not create false Range failures.
  - Windows package runtime launch keeps no-browser/background behavior and restores runtime stdout/stderr logs for diagnosability.
- Verification:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600`
  - Result: `PASS`.
  - Checks passed: `7/7`.
  - Evidence: `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`.

## S09C Final Package Evidence Refresh

- Entry:
  - After the release-package CDP smoke found that Markdown image paths could render as previews without entering the actionable artifact shelf, `MessageContent` was hardened so inline image paths become legacy image artifacts and image artifacts with preview sources remain available for preview/retouch even if local stat falls back to preview-only.
  - The release runtime sanitizer previously spent too long scanning third-party vendor trees. It now keeps business/runtime files under strict sanitization while skipping full-text scans for vendor trees such as Python site-packages, Node, Playwright browsers, and wheelhouse, and skips very large text files.
- Final rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551259992`
    - SHA256: `058C7BAC58592664A5F2FAB952A3FFACD1CC7126BF0EC905F6C117351AAECF4D`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652435170`
    - SHA256: `7C52854B6909BC16DED8CC848CE83EA2DB20A41E45F2572AD5D8D910346401DB`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204941831`
    - SHA256: `7ECC0C459AD22F3627D9E8B27C69A4DBB746CB51C4AF4A4495A892C0C6869494`
- Release-index status:
  - `deploy/ecorex-site/release-index.json` status: `ready`.
  - Manifest hash in release-index: `52790D0AB712ABA44439854E2ED8FA6E45F549099C954EA6EAC358C4F6DCF701`.
  - Signatures are marked `not-required` for WebUI packages because this iteration intentionally does not develop/sign the desktop app.
- Final verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `19 passed`
  - `npm run typecheck`
  - `npm run build:renderer`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 180` -> `PASS`

## S10 Retouch And Infinite Canvas Completion

- Entry:
  - User requested the independent retouch/infinite-canvas slice after v0.3.0 release/update hardening.
  - Required capabilities: rectangle selection, lasso/circle selection, text annotations, uploaded image references, current-round artifact panel, multi-image selection, and T text-edit flow that routes to imagegen rather than local bash/Python editing.
- Changes:
  - `ImageRetouchCanvas` now uses a typed annotation layer model: `arrow`, `rect`, `lasso`, `text`, and `image`.
  - Bottom toolbar now exposes arrow, rectangle selection, lasso/circle selection, T text target, hand/pan, zoom, upload reference image, and undo/erase.
  - Uploaded images are added as visible reference layers on the infinite canvas and included in the transparent marker export.
  - Right-side `本轮产物` panel lists images from the current assistant response; if only one image is available, the current image is auto-added.
  - Users can select multiple images before submission. The generated draft includes all selected original image paths.
  - T text targets generate prompt constraints to preserve original font style, color, shadow, perspective, and layout while changing text content through imagegen.
  - The transparent marker layer still does not draw or mutate the original image; it only draws user annotations and uploaded reference images.
- Verification:
  - `npm run typecheck`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `18 passed`
  - `npm run build:renderer`
  - Vite preview served the built renderer at `http://127.0.0.1:5174/` and returned the built root document; the preview process was stopped afterward.
- Known follow-up:
  - Automatic OCR detection for all text boxes is not wired because the current WebUI has no production OCR endpoint. The T flow is implemented as user-placed text targets and imagegen style-preserving text edit instructions.

## S02 Active Turn Control

- Frontend send API now sends `interrupt_mode` with `/message`.
- Supported modes: `replace`, `amend`, `queue`, `branch`.
- Default composer send while the same session is running uses `replace`, displayed as updating the current task.
- Explicit running-task menu added to composer:
  - `更新任务`
  - `排队稍后执行`
  - `新开分支`
- `新开分支` creates a new WebUI session locally and sends the new message there while the original task keeps running.
- Queued message action UI no longer shows the ambiguous primary `引导` action.
- Queued messages now expose concrete actions:
  - `提到队首`
  - `取消排队`
- Backend `/message` now parses `interrupt_mode`.
- Backend default same-session behavior is active-turn control:
  - `replace` and `amend` cancel the active request and wait for the session lock.
  - `queue` explicitly accepts the message into the session queue.
  - `branch` refuses same-session admission so the frontend must use a distinct session.
- Fixed a concurrency edge: explicit queue/branch no longer increments the replacement ticket and therefore cannot supersede an active replacement wait.
- Verification so far:
  - `python -m py_compile channel/web/web_channel.py`

## S01 Version Metadata And Release Copy

- Updated current WebUI-facing version metadata to `0.3.0`:
  - `cli/VERSION`
  - `desktop/package.json`
  - `desktop/package-lock.json`
  - `common/ecorex_release_notes.py`
  - `deploy/ecorex-site/index.html`
  - `deploy/ecorex-site/install-webui.ps1`
  - `deploy/ecorex-site/install-webui.sh`
  - `deploy/ecorex-site/admin/index.html`
  - `deploy/ecorex-site/admin/admin.js`
  - `channel/web/web_channel.py`
- Release notes title set to `EcoreX 0.3.0 生产级任务控制与在线更新稳定性版本`.
- Added `deploy/ecorex-site/release-index.json` as the v0.3.0 release-index contract.
- Updated `deploy/ecorex-site/manifest.json` with:
  - current version `0.3.0`
  - `releaseIndex`
  - signature trust metadata
  - rollout metadata
  - kill-switch metadata
  - rollback metadata
  - online-update state machine metadata
- Marked v0.3.0 downloadable artifacts as `pending` with `sha256: pending` until the release orchestrator builds and validates real packages.
- Preserved v0.2.9.2 only as backward-compatible Web client keys and rollback previous version metadata.
- Verification:
  - Parsed `deploy/ecorex-site/manifest.json` and `deploy/ecorex-site/release-index.json` with Node JSON parsing.
  - Searched active WebUI release files for remaining `0.2.9.2` references; remaining references are compatibility keys or rollback metadata.

## S11 Final v0.3.0 Seal And Re-acceptance

- Entry:
  - Multi-agent review found two additional product issues during final acceptance: queued messages could show queue guidance without rendering the `提到队首` / `取消排队` action buttons, and the release CDP smoke did not yet cover active-turn UI paths.
  - User also requested domestic GitHub mirror as the first download source; this remains in the final manifest source order.
- Fixes after review:
  - Queued assistant messages now keep queued action state through a `queued` recovery fallback, so queue actions render even when stream/preflight merging drops the original transient `sendAttempt`.
  - CDP release smoke now validates:
    - `查看更多(N)` and `收起` session list behavior.
    - stop action preserves scroll position.
    - long composer input autosizes without page jump.
    - default active-turn send uses `interrupt_mode: replace`.
    - explicit `排队稍后执行` uses `interrupt_mode: queue` and `取消排队` is clickable.
    - explicit `新开分支` sends `interrupt_mode: branch`.
    - inline local image previews render with non-zero natural size.
    - precise retouch/infinite canvas supports two-image selection, text target, rectangle, lasso, uploaded reference image, marker attachment, and imagegen-only draft.
- Final effective package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551265395`
    - SHA256: `1E9050CF15E1FF3169CA3805B8639FE0AC9A4B984C4AD2F02FCAC4AA9AF15522`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652441202`
    - SHA256: `3063898AF17593162F9EC3B876941BC63E60225A0E71209A7B058B522063ED37`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204953267`
    - SHA256: `73FE83EF80BAD5A2B6C92CB6430F6848DCDED1A6D2810E9596B63DF78EF89471`
- Final verification:
  - `npm run typecheck` -> pass.
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `22 passed, 1 warning`.
  - `npm run build:renderer` -> pass, built renderer asset `index-C7zTVepT.js`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` -> pass; `deploy/ecorex-site/release-index.json` status `ready`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-runtime-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 300` -> `PASS`.
- Final residual environment gates:
  - Credentialed external connector quick panel and post-update preservation still require a machine with real configured MCP/connector credentials.
  - Destructive online-update rollback on failed service or failed external-connector health check is implemented and smoke-policy covered, but not executed against a credentialed production-like machine in this workspace.
  - Automatic OCR text-box detection for retouch T mode still needs a production OCR endpoint; current v0.3.0 supports user-placed T text targets with style-preservation imagegen instructions.

## S12 Final Shell Polish And Release Re-seal

- Entry:
  - User clarified that the composer top divider should be removed, while the rounded top shell belongs to the main chat/session panel boundary.
- Changes:
  - Removed the extra top divider above the composer area.
  - Added the rounded/top-bordered main chat panel shell and matching rounded header treatment, with mobile reset.
- Final rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551265386`
    - SHA256: `3BF9A6546A294C2A96AC32786B7B893848BF3DC6A30897F228AD13CCDC49A48C`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652441190`
    - SHA256: `1E7EC8295EEE2736A711AD1C152CB7EABF32A12D61C27DF5CC8EF060A211AA28`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204953245`
    - SHA256: `D4E9E0D2EDE0AA200818F9D20E628AB7A9456A962E00EDA1C7DC4A69714543AF`
- Final verification after visual polish:
  - `npm run build:renderer` -> pass, built renderer assets `index-VHJzJbCn.js` and `index-PsAbIX8T.css`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` -> pass; `deploy/ecorex-site/release-index.json` status `ready`.
  - Synced Windows, macOS, and combined v0.3.0 WebUI packages into `deploy/ecorex-site/downloads/` and wrote `.sha256` sidecars.
  - Verified `deploy/ecorex-site/downloads/` package size/SHA256 against `deploy/ecorex-site/manifest.json` and `deploy/ecorex-site/release-index.json` for `webui-windows-x64` and `webui-macos-universal`.
  - Pushed branch `codex/ecorex-v0.3.0-hardening` to `origin`.
  - Created source release `https://github.com/zhangyifanjackson-dotcom/EcoreX/releases/tag/v0.3.0`.
  - Created installer asset release `https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/tag/v0.3.0`.
  - Uploaded Windows, macOS, combined WebUI packages and `.sha256` sidecars to the installer asset release.
  - Verified the manifest primary mirror (`ghproxy.net` -> `EcoreX-installers/releases/download/v0.3.0`) returns HTTP 200 and expected `Content-Length` for all three packages.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-runtime-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 300` -> `PASS`.
- Evidence:
  - `docs/v0.3.0/artifacts/webui-hardening-verification.json`
  - `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`
  - `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`
  - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.json`
  - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.png`
  - `docs/v0.3.0/artifacts/github-release-v030.json`

## S12 User-Observed Hotfix: Image Preview, Update Notice, And Multi-Image Count

- Entry:
  - User reported in the installed v0.3.0 WebUI that image generation could take about 10 minutes without visible return, then the generated image row rendered broken.
  - The update banner kept showing an older `EcoreX 0.2.9.2` admin notice after the runtime was already on `0.3.0`.
  - Generated/retouch artifacts showed a duplicate `preview only` row with only the filename; choosing that row could open an empty precision-retouch canvas.
  - User also reported that imagegen used to support generating two images at once, but the new build only produced images one by one.
- Root causes:
  - Provider HTTP waits still used 300s per provider route and fallback could compound the perceived wait.
  - Runtime-generated images under `runtime-*/images` were valid PNGs but outside `/api/file` preview roots, so the frontend could not load them.
  - The artifact shelf merged legacy filename-only image detections beside absolute-path artifacts and allowed retouch from preview-only rows.
  - `OpenAIProvider._create` hard-coded `n: 1`; direct imagegen schema did not expose `n/count/output_count`, and Web image-job API expanded `output_count=2` into two one-image tasks.
  - Admin release notices were shown as update-state banners even when the notice version was older than or equal to the running version.
- Changes:
  - Added dismissible update-state banners and filtered stale `admin-release-notice` versions at or below the running app version.
  - Added runtime `images/` as an internal read-only preview root, with file-stat/file-serve bypassing the workspace permission broker only for that internal generated-image root.
  - New relative `output_dir=images` resolves to the user workspace rather than the runtime install directory.
  - Artifact shelf now removes bare filename-only duplicate image rows when a concrete local/absolute image artifact with the same basename exists, hides missing source-only images after verification, and only enables precision retouch for verified ready images.
  - Restored native multi-image count: direct `imagegen` schema accepts `n`, `count`, `output_count`, and `num_images`; provider runner and standalone script pass the count; OpenAI sends `n` to generations/edits; non-native providers are looped inside imagegen rather than routed through bash.
  - Imagegen output files from OpenAI multi-image responses now include ordered `01`, `02`, etc. filename segments and artifact indexes.
  - Web image-job no-tasks API now creates one task with `output_count=count` instead of splitting the request into many one-image tasks.
- Verification:
  - `python -m py_compile desktop\runtime\ecorex-runtime\channel\web\web_channel.py desktop\runtime\ecorex-runtime\channel\web\files.py desktop\runtime\ecorex-runtime\agent\tools\imagegen\imagegen.py desktop\runtime\ecorex-runtime\agent\tools\imagegen\provider_runner.py desktop\runtime\ecorex-runtime\skills\image-generation\scripts\generate.py` -> pass.
  - `npm run build:renderer` -> pass.
  - Mocked OpenAI provider smoke with `n=2` -> pass, generated two files named with ordered `01` and `02` segments.
  - Internal preview-root import smoke -> pass for `runtime/images`.
  - `git diff --check` -> pass.
- Residual:
  - The currently running installed runtime must be refreshed through the rebuilt package or update flow before these source fixes affect that live browser tab.

## S13 Public Release Mirror Re-seal

- Entry:
  - User requested the download first source to prefer domestic GitHub mirrors for faster WebUI package updates.
  - Online manifest still exposed the domestic mirror URLs as generic `asset-base` entries, so the user-facing update chain could not clearly treat them as the primary mirror tier.
- Changes:
  - Extended public-release mirror classification so `ghproxy.net` and `ghfast.top` are recognized as `github-release-cn-mirror`, alongside the previous `gh-proxy.com` pattern.
  - Repacked `release-artifacts/EcoreX_0.3.0-public-release.zip` with download mode `github-cn-primary` and mirror order: `ghproxy.net`, `ghfast.top`, GitHub origin fallback.
  - Redeployed the public release with WebUI large package upload skipped, preserving manifest-mirror delivery for Windows/macOS WebUI packages.
- Published public zip:
  - `release-artifacts/EcoreX_0.3.0-public-release.zip`
    - Size: `2874502`
    - SHA256: `F1A9E84796E13020EBEF6884F3662FAE5E531F0AB96DF671382295844DF8AB69`
- Verification:
  - `tar -xOf release-artifacts\EcoreX_0.3.0-public-release.zip site/manifest.json` -> `download.mode = github-cn-primary`; mirrors are `ghproxy.net`, `ghfast.top`, GitHub origin fallback.
  - `python scripts\deploy-v024-production.py --promote-public-release` with `ECOREX_SKIP_WEBUI_DOWNLOAD_UPLOAD=1` -> `PASS`.
  - Online `https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json` -> version `0.3.0`, `download.mode = github-cn-primary`.
  - Online mirror HEAD checks -> `ghproxy.net` and `ghfast.top` return HTTP `200` and expected `Content-Length` for both `webui-windows-x64` and `webui-macos-universal`.
- Evidence:
  - `docs/v0.3.0/artifacts/public-release-mirror-online-check.json`
  - `docs/v0.3.0/artifacts/production-deploy-online.json`

## S14 Capability Center / Skill Hub Slice And Rejected Home Capture

- Visual source of truth:
  - Superseded: the first five-robot implementation capture was rejected by the user as not being the latest main UI.
  - `resources/homepage.png` is a legacy AionUI screenshot and is explicitly excluded.
  - `docs/v0.3.0/e-mate-five-robot-home-reference.png` is retained only as rejected implementation evidence and must not be used as visual truth.
  - Fixed the GA Runtime projection so the visible shell reports `e-Mate v0.3.0`, never mock `v1.0.0`.
- Capability Center acceptance support:
  - Added realistic GA Skill Hub cards and query/category filtering so browser acceptance exercises the real card hierarchy instead of an error-only screen.
  - The card projection includes slug, immutable version/digest, tags, category, privacy-safe author, e-Mate provenance, local install state, and readiness.
- Root-cause Hub/Extension fixes from the parallel audit:
  - Hub publish/install and legacy migration now share the exact 11-slug exclusion list.
  - Exact aliases (`docx/xlsx/pptx/pdf/lark-cli`) and same-slug native providers bind the existing `skill.*` authority; no duplicate `hub.*` install is created.
  - Downloaded CAS metadata version must equal the requested immutable version.
  - Empty/account-id/email-like author nicknames project as `e-Mate 用户`; author refs remain HMAC-derived.
  - Search/read/run authorization now carries the exact Extension state revision as well as generation, so configure/disable/uninstall/re-enable invalidates stale grants with `skill_state_changed`.
  - Node `.mjs` entry parsing matches the declared Runner contract.
- Verification:
  - Frontend v1 suite: `206 passed, 0 failed`.
  - Skill Hub/Runner/Runtime focused Python suite: `43 passed`.
  - Parallel Extension/Hub audit suite: `60 passed, 1 skipped`; contract generation, upstream lock validation, ruff, and py_compile passed.
  - This visual acceptance is invalidated; functional checks remain useful, but both themes require a new same-viewport comparison.

## S15 Confirmed e-Mate 2.1.47 Home Source

- The user confirmed `C:\e-Mate-正式版\.tmp\e-mate-latest-home-2.1.47.png` as the main-screen visual baseline for both light and dark themes.
- The source of truth is the current renderer implementation, not a reconstructed mock:
  - `packages/desktop/src/renderer/pages/guid/GuidPage.tsx`
  - `packages/desktop/src/renderer/pages/guid/index.module.css`
  - `packages/desktop/src/renderer/pages/guid/components/GuidInputCard.tsx`
  - `packages/desktop/src/renderer/pages/guid/components/GuidUsageOverview.tsx`
  - `packages/desktop/src/renderer/components/layout/Layout.tsx`
  - `packages/desktop/src/renderer/styles/themes/e-mate-standard.css`
  - `packages/desktop/src/renderer/assets/e-mate-team-hero-transparent.png`
- Porting rule: remove Electron-only titlebar/window chrome, retain the v0.3.0 WebUI Composer and Settings behavior, and reproduce the confirmed hierarchy, spacing, colors, five-robot asset, sidebar, rounded content surface, and usage overview in both themes.

## S16 Confirmed Home Port — First Visual Convergence

- Ported the confirmed 2.1.47 home hierarchy into the existing React WebUI without adding a UI dependency:
  - five-robot asset is byte-identical to the source SHA-256;
  - source title/subtitle, 1088px centered home column, 24px input card, project footnote, four metrics, seven-day trend, recent task and summary hierarchy;
  - 248px sidebar, inset 16px rounded workspace, Creative Center and Capability Center entries, home-selected New Task row;
  - source light/dark surface palette and orange brand palette, with contrast-only adjustments to subtle text and dark success state.
- Preserved the v0.3.0 Composer behavior: attachments, connectors, Luna High model selection, permission control, send/interrupt, project binding and Settings remain live. Electron titlebar/window controls were intentionally not imported into WebUI.
- Creative Center reuses the existing six real template actions as a sidebar workspace instead of leaving duplicate cards below the source home screen.
- Visual QA used the selected in-app Browser at 1440×900 and compared the source and candidate in the same tool input for both themes. Remaining intentional difference: WebUI has no Electron titlebar; task data and retained Composer controls are real v0.3.0 projections rather than copied screenshot values.
- Verification: Runtime contract check passed; TypeScript passed; production Web build and content-addressed bundle gate passed; full frontend suite 206/206 passed.
- Deliberately not claimed complete:
  - Cow seed still needs a per-package immutable CAS lock; current upstream lock is source-level only.
  - Signed one-time install intent, append-only global install log, tag/source filters, version history, authenticated upload/download and same-origin Runtime installation are complete. The full-size React Capability Center remains the device management surface; the fixed upstream Astro card/page structure is also built at `/ecorex-agent/skills/` and reuses the same Runtime bearer, CSRF bootstrap and Hub API. It is a signed static WebUI asset, not a second service, OAuth system, database or storage authority.
  - A running Agent batch does not hot-swap its entire Extension snapshot; stale calls are rejected immediately and the next snapshot sees the change.
  - Production Python/Node Skill execution remains blocked on a signed AppContainer/Seatbelt protocol that attests CAS read roots, runtime digest, and declared domain policy.

## S17 Controlled Skill Runner — Production Boundary Re-audit

- Decision:
  - Keep production script Skill execution fail-closed. The existing signed sandbox primitives are not sufficient to implement the requested Python/Node Runner without either widening their trust boundary or routing through the generic `shell` capability.
  - No host subprocess or generic shell fallback was added. The existing frozen `ControlledSkillRunRequest`, exact Extension revision/generation fences, `search → read → run` authorization, empty v1 parameter contract and bounded Runner result remain the only executable adapter boundary.
- Verified reusable primitives:
  - The Windows native helper already provides a Job Object with kill-on-close, active-process, process/job memory and CPU caps; timeout, transport failure and output overflow terminate the whole Job Object (`platform-staging/native/windows/ecorex_sandbox_process.cpp`).
  - The Windows launch protocol attests the helper, signed slot/security receipt, workspace-root digest and exact Python artifact digest before execution.
  - The macOS Seatbelt backend has a behavioral probe, workspace-scoped writes, denied network and a process-tree probe (`ecorex/integration/sandbox.py`).
- Exact production blockers:
  - Windows protocol `ecorex-sandbox-launch-v1` accepts exactly three child arguments: `<python> -I <artifact>`. It rejects Node and any script arguments before launch.
  - Windows `read_roots` is fixed to the signed Runtime `payload/` directory. The managed Skill CAS is `runtime.db` sibling `extension-cas/`, outside that signed read root. Supplying CAS as a workspace would grant the package write authority and change the signed workspace/security receipt, so it is not a valid immutable-code workaround.
  - The native helper launches only an artifact contained by an attested read root and verifies only that one file's SHA-256. It does not attest the full normalized CAS package tree, imported references/modules or the selected Skill revision.
  - Windows networking is only `workspace-write + deny` or `danger-full-access + allow`; macOS always denies network. Neither backend can enforce a normalized per-domain allowlist.
  - macOS permits host-wide file reads and only implements Python `-I <artifact>`; it has no signed Node runtime identity, exact CAS tree attestation or domain-aware broker.
  - The verified `sandbox` Capability Pack exposes only generic `shell`, and `ProcessCapabilityPackAdapter` invokes the OS backend only for that tool. Reusing it would create the explicitly forbidden host-Shell bypass instead of a typed `skill_run` protocol.
  - The verified backend and its attestation receipt are private to Pack resolution. Production `compose_extension_service` and server composition do not expose or bind a `ControlledSkillRunner`, so script Skills correctly project `unsupported/controlled_runner_unavailable`.
  - `skill-runtime.json` schema v1 declares no parameter schema. Arbitrary script parameters therefore cannot be validated and remain rejected rather than serialized into an untrusted command line.
- Minimum signed upgrade required before enabling production execution:
  - Add a typed, signed Skill-process protocol (not `shell`) that binds extension ID, exact state revision/generation, immutable CAS tree digest, entrypoint digest and interpreter identity.
  - Stage or attest a read-only CAS package root, add signed Python and Node identities, preserve timeout/output/process-tree enforcement, and define effect-specific file authority.
  - Add a domain-aware egress broker/policy, or explicitly support only networkless Skills until such a broker exists.
  - Expose the verified adapter through production server composition and re-run the state fence immediately before spawn, during controlled waiting and after process completion.
- Verification:
  - Existing Extension tests already prove missing Runner remains `unsupported/controlled_runner_unavailable`, non-empty undeclared parameters are rejected, the adapter receives no command string, and configure/disable/uninstall/re-enable invalidates stale grants.
  - Focused Runner/state suite: `3 passed`; signed sandbox source/resolver/output-flood/timeout suite: `5 passed`.
  - Source audit confirmed the boundary in `ecorex/integration/sandbox.py`, `ecorex/integration/pack_process.py`, `ecorex/server/pack_resolver.py`, `release/capability-packs/sandbox/sandbox_pack.py`, and `platform-staging/native/windows/ecorex_sandbox_process.cpp`.

## S18 Cow Seed Public Package Audit

- Re-downloaded the fixed Cow Hub source archive for commit `0c214c3a61f66f8c122111c23270bd146241001b`; the observed `836019` bytes and SHA-256 `2926c9b72da1269b1a5676802932a2551f53bcf3162be8e0acef2a823c1a5b18` exactly match the existing source lock.
- The current public catalog still contains all 53 selected slugs at the locked versions. Source distribution is 33 registry, 6 GitHub and 14 uploaded ZIP entries; all 53 report a mirror, but zero entries publish a package SHA-256.
- The fixed upstream implementation proves the download endpoint is not immutable: `POST /api/skills/{slug}/download` accepts a slug and optional `mirror`, then reads the current database row/R2 key. It accepts neither version nor digest. GitHub fallback exposes a branch/path URL, and ClawHub/LinkAI fallbacks are dynamic slug download URLs.
- Captured all 53 current mirror ZIP responses into a temporary directory only (`7,470,533` bytes total) and computed their real package SHA-256 values. Three representative immediate re-downloads were byte-equal, which is useful transport evidence but does not prove future immutability.
- Offline ingestion through the current e-Mate `LocalSkillBundleStore` produced zero release-ready packages:
  - 51 packages were rejected by the normalized CAS contract: 20 missing an exact root `SKILL.md`, 14 unknown frontmatter, 8 non-static resources, 4 undeclared script namespace entries, 2 executable resources, 2 unbounded descriptions and 1 script package without `skill-runtime.json`.
  - `markdown-converter` and `discord` normalized, but both reported version `0.0.0`, not catalog versions `1.0.0` and `1.0.1`; the version-bound gate correctly rejected them.
- Result: `seed_packages_locked` remains false and the existing 53-item release gate remains fail-closed. Temporary upstream bytes were not copied into the repository or user directories. No deployment or online mutation occurred.
- Full per-package observed sizes, SHA-256 values and exact CAS rejection/version evidence are retained in `docs/v0.3.0/skill-hub/seed-public-source-audit.json`. They are audit evidence, not a seed lock, because the bytes are not persisted and the upstream endpoint does not publish immutable identities.

## S19 Final Backend Completion Audit

- Audited seven non-UI, non-release requirements against production composition and focused tests rather than presence-only source checks.
- Achieved:
  - central automatic image create/edit/deliverable routing, suppressions and bounded attachment/recent-image follow-up context;
  - production default `full_access`, durable one-time migration, `danger-full-access`/`never`, preserved user downgrade and verified legacy broker bridge;
  - empty post-tool continuation without replay or fabricated completion, recoverable verified Feishu failure and distinct-target batch convergence;
  - one Usage/Audit/account projection with shared KPI/reconciliation facts;
  - bearer-principal self-service password change, normalized account/email login and all durable device-lease revocation with Runtime local logout/reload.
- Incomplete, not hidden as complete:
  - Luna high is the only v1 default and GPT-5.5 is absent from the active v1 catalog/Control Plane/WebUI, but executable legacy model-capability compatibility, legacy tests and several smoke scripts still contain GPT-5.5. This exceeds historical-record deserialization-only compatibility.
  - Skill behavior works at both layers, but `agent/skills/manager.py` remains a second live `skills_config.json` enablement authority. Extension generation changes reject stale calls immediately, while a running Agent batch still needs a fresh Extension snapshot to discover a newly installed Skill.
- No requirement in this audit is wholly missing. No backend code was changed because both residuals require intentional compatibility/authority migrations, not a safe one-line repair.
- Verification: 101 focused tests passed across image routing (1), model policy (39), permissions (23), Skills (11), continuous execution (4), Usage/Audit (15) and password/session revocation (8).
- Evidence matrix: `docs/v0.3.0/backend-completion-audit.md`.

## S19 Final Visible Brand And Release-Surface Audit

- Audited the main WebUI, Settings/Composer, Runtime error/status projections, Capability Center built-ins, connector authorization result, management page, download page, public v0.2.9.2-compatible manifest, and GitHub release presentation.
- Changed remaining user-visible `EcoreX` copy to `e-Mate` in Runtime read-only/restart/uncertain-execution messages, visual-evidence context, system health summaries, connector authorization completion, built-in Extension display names, MCP client presentation, FastAPI product titles, Control Plane model validation/probe text, and GitHub release title/body.
- Preserved compatibility identifiers exactly where required: `X-EcoreX-*` headers, `ecorex-*` storage/document keys, `/ecorex-agent/*` routes, local model IDs, `EcoreX_0.3.0-*` artifact names, installer/download URLs, legacy install/data directories, and machine product IDs.
- Confirmed the legacy public manifest exposes `product: e-Mate`, `version: 0.3.0`, e-Mate notes/source text, and no `core_version` field.
- Confirmed repository search has no `1.0.18`; the only `core_version` match is the negative API contract assertion `assert "core_version" not in version.json()`.
- Regression guards now reject visible `EcoreX` title/body/ARIA text in the public download and management HTML and expect e-Mate GitHub release metadata/system health copy.
- Verification:
  - Focused Python brand/release/runtime suite: `55 passed, 1 skipped`.
  - Extension/connector integration checks: `48 passed`; model activation: `16 passed`.
  - Management/download/model suite: `38 passed, 1 deselected` (wheel-only test omitted because the workspace `.venv` has no `pip`).
  - Frontend visible-brand contract: `1 passed`; TypeScript typecheck passed.
  - Ruff on all touched Python and tests: passed.
  - Final visible-text scan: `NO_VISIBLE_ECOREX_MATCHES`.

## S20 WebUI-only Skill Hub Browse And Install Entry

- Reused the existing full-size React Capability Center, authenticated Runtime bridge, Control Plane registry/CAS, and device session; no Electron or new web framework was introduced.
- Completed the WebUI path for discovery/search, authenticated detail with version history, ZIP upload/publication, verified ZIP download, and install-and-enable.
- Package downloads now flow through `/api/v1/skill-hub/skills/{slug}/versions/{version}/package`; the Runtime validates slug/version, resolves the exact immutable version through the authoritative Hub detail projection, checks the CAS digest identity, and returns an authenticated attachment without caching.
- The detail dialog shows slug, version, author, readiness, immutable digest, original-source traceability and version history. Download uses the browser-native attachment path. Install continues through the existing account/slug/version/digest-bound single-use install intent and completes locally through ExtensionService.
- Exact WebUI boundary: `emate://skills/install` cannot be registered or owned by a browser-only deployment. The WebUI therefore installs through its authenticated same-origin Runtime and does not render or pretend to invoke an OS deep link. Native protocol registration remains a future desktop-installer responsibility, not a WebUI fallback.
- Verification:
  - Skill Hub registry/transport/Runtime API: `6 passed`.
  - Runtime contract generation/check and TypeScript typecheck: passed.
  - WebUI Skill Hub contract: `5 passed`.
  - Full frontend suite after integration, including deferred boundary identity: `208 passed`; production content-addressed bundle gate: passed at `149.87 KiB` initial gzip JS.

## S21 Final Frontend And Capability Center Completion Audit

This audit was source/test/build based. Browser visual QA was intentionally left to the main Product Design acceptance pass.

| Acceptance surface | Result | Evidence |
|---|---|---|
| Visible e-Mate brand | Pass | Main WebUI, management and download visible-text scan returned `NO_VISIBLE_ECOREX_MATCHES`; product-language brand contract passed. Download fallback/ARIA version was corrected from stale `v1.0` to `v0.3.0`. |
| Confirmed five-robot home | Pass | Product asset and confirmed 2.1.47 source asset both hash to `C7F395A0729245C083F0FB32E0A1644AB3244E0986D4397385AFA233AA0ADF61`; `HomeDashboard` remains lazy-loaded into the new-conversation surface. |
| Composer and Settings retained | Pass | `AppV1` still renders the existing `Composer` and lazy `SettingsDialog`; attachment/model/connector/permission/send and account/password/system/update/permission wiring remains. TypeScript and production build passed. |
| Real-event AICSS states | Pass | Reasoning and public tool-call state come from real Item projections; search/image animation keys off actual status; streaming batches deltas once per frame and flushes terminal facts synchronously. Timeline contract passed. |
| Task List | Pass | `task_list.updated` reduces into durable items and `TaskListBlock` renders actual status; reducer suite passed `19/19`, including no false completion of pending work. |
| Creative / Capability Center | Pass | Sidebar entries open real workspaces; Creative templates use the retained Composer prefill guard; Capability Center owns Discover/Installed/Custom tabs. |
| Skill Hub discover/search/category/tag/source | Pass after minimal fix | Runtime/Control Plane already accepted `tag` and `source`; WebUI now exposes and forwards both filters with query/category. |
| Skill Hub detail/upload/download/install | Pass | Authenticated detail/version history, canonical ZIP publication, digest-bound download and one-time-intent install/enable paths are present; Hub Python slice passed `6/6`. |
| Skill configuration/uninstall | Pass after minimal fix | Secrets stay in password inputs and Runtime configuration; Extension detail now renders the backend-projected `uninstall` action with existing confirmation and disabled-reason authority. |
| Dark/light, responsive, accessibility | Source-contract pass | Theme/contrast, density, forced colors, focus, reduced motion, coarse pointer and narrow-layout contracts passed `19/19`. Same-viewport browser comparison remains the main-agent gate. |

- Verification:
  - TypeScript and generated Runtime contract check: passed.
  - Frontend brand/Skill Hub/message-flow contracts: `23 passed`.
  - Accessibility/theme/responsive source contracts: `19 passed`.
  - Runtime reducer/Task List: `19 passed`.
  - Production Vite build/bundle gate: passed; `33` content-addressed assets, entry gzip `14.99 KiB`, initial JS gzip `149.96 KiB`.
  - Public download fallback-version tests: `2 passed`.

## S22 Product Design Browser Acceptance

- Used the confirmed `e-mate-latest-home-2.1.47.png` and current `C:\e-Mate-正式版` source as the visual authority; rejected the earlier stale home reference.
- Compared reference and implementation together in the in-app Browser at `1440 × 900`, DPR 1. Captured final dark/light evidence in `docs/v0.3.0/artifacts/design-qa/`; browser console remained empty.
- Restored the two source footer rows (`设置`, `用户中心`), aligned the e-Mate lockup, and restored the home connection dot/orange action group.
- Browser interaction exposed a root bug not covered by the source contract: entering Creative Center unmounted Composer, so a local draft was lost before the no-overwrite guard ran. Composer draft ownership now lives in `AppV1`; template selection returns home, prefills only an empty draft and focuses the textarea.
- Browser evidence: empty draft became `帮我创作一张图片：`; existing `已有草稿` survived a different template selection unchanged; Settings/password and Capability Center discovery/detail paths opened successfully.
- Verification: TypeScript passed; Product Language contract `10/10`; production bundle gate passed with 33 content-addressed assets and `149.98 KiB` initial gzip JS.
- Full record: repository-root `design-qa.md`; final result passed.

## S23 Fixed Cow Seed Resolution Lock

- Replaced the indefinite 53-package pending gate with a complete, offline disposition lock. Every selected catalog item is bound to the source ZIP size and SHA-256 observed in the fixed 2026-08-04 capture; the strict 11-slug exclusion set remains unchanged.
- Five exact aliases are release-usable without installing duplicate code: `docx → skill.office-documents`, `xlsx → skill.office-spreadsheets`, `pptx → skill.office-presentations`, `pdf → skill.office-pdf`, and `lark-cli → skill.feishu-lark`.
- The other 48 entries are explicitly `unsupported`, with a bounded reason code derived from the actual CAS rejection or version mismatch. They are not downloaded, exposed as installable packages, or given invented runtime, dependency, environment, command, network, or permission declarations.
- No normalized mirror ZIP is published in this slice. The previously audited upstream bytes were not retained, the live Cow/registry endpoints do not expose immutable version/digest identities, and the current network could not reliably reproduce the captured bytes. Creating replacement packages from descriptions or stale metadata would be a false provenance claim, so `mirrored_package_count` is honestly `0`.
- The resulting release gate is nevertheless complete rather than pending: 53/53 entries have a terminal resolution, all source identities are locked, aliases bind existing e-Mate authorities, unsupported entries fail closed, and `network_dependency=false`. Runtime startup and release validation do not consult Cow.
- Added a deterministic lock builder and hardened validator. Alias-target tampering, unknown refusal reasons, excluded slugs, source identity defects, missing mirrored bytes, CAS/version drift, and summary-count drift fail closed.
- Verification: seed builder/validator tests `3 passed`; Ruff passed. No deployment or user-directory mutation occurred.

## S24 Windows x64 WebUI Package / Receipt / Upgrade Execution Audit

- Scope was local Windows x64 only. No upload, remote mutation, public manifest change, signing operation or deployment was performed.
- Preflight:
  - workspace resolved to `C:\EcoreX-Agent生产版`;
  - C: free space before the run was `114,458,787,840` bytes;
  - `release-artifacts/` and a current v0.3.0 Windows package/receipt were absent;
  - the historical `scripts/prepare-ecorex-webui-local-release.ps1` is absent from the current checkout;
  - `scripts/release-ecorex-webui-orchestrator.ps1` is an intentional retirement stub. Executing it returned `78` with the instruction to use the signed ReleaseBuilder.
- Current-source Web build:
  - command: prepend `C:\EcoreX-Agent生产版\.venv\Scripts` to `PATH`, then run `npm run typecheck` and `npm run build` from `desktop/`;
  - result: passed, including generated-contract check, TypeScript, Vite, content rehashing and the production bundle gate;
  - `desktop/dist` canonical ReleaseBuilder identity: SHA-256 `9b726f78286dca7752f371aa38e38fdb1213b96c2b92245f312818e70c5a51c8`, `34` files, `2,231,098` bytes;
  - bundle gate reported entry gzip `15.01 KiB`, initial JS gzip `149.98 KiB`, deferred feature gzip `49.82 KiB`, and `29` chunks.
- Fixed v0.2.9.2 Windows baseline input:
  - public manifest reported `EcoreX_0.2.9.2-webui-windows-x64.zip`, `550,842,181` bytes, SHA-256 `992C44710543D70AFD5B3F90680097F4745F4DD33E631E0DB1F01747BFA9F17E`;
  - downloaded with bounded retry/resume to `tmp/v030-windows-package-input/EcoreX_0.2.9.2-webui-windows-x64.zip`;
  - local size and complete SHA-256 matched exactly before any ZIP inspection;
  - the current-source July v0.3.0 assets were not downloaded or reused.
- Package-layout/root-cause result:
  - the fixed baseline contains the old installer, which launches `runtime/app.py`;
  - current source `app.py` is an intentional exit-78 tombstone and permits production launch only through a Bootstrap-verified slot using `python -m ecorex.server serve`;
  - the only current signing builder (`scripts/build-v1-direct-operator-release.py`) requires exact Windows stage provenance, dependency-lock evidence, independent release/publication keys and protected-gate inputs; no current Windows stage or receipts exist in this workspace;
  - therefore overlaying current source into the old package would create a ZIP that builds but cannot start, while fabricating a legacy receipt would bypass the signed v1 authority. The run correctly stopped without producing `EcoreX_0.3.0-webui-windows-x64.zip` or a build receipt.
- Verification actually executed:
  - `npm run test:v1` from `desktop/`: `209 passed`;
  - `pytest -q tests/v1/test_legacy_webui_manifest.py tests/v1/test_runtime_update_service.py tests/v1/test_update_activation_health.py tests/v1/test_recovery_execution_lane.py`: `42 passed`, one dependency deprecation warning;
  - these prove the Web bundle and update/hash/activation/rollback protocols, but are not substituted for an actual package upgrade.
- Release blocker:
  - produce a current signed Windows x64 stage and Bootstrap-compatible installer/receipt through the protected ReleaseBuilder pipeline, then package the canonical Web bundle and run `smoke-v030-webui-package-runtime.ps1` plus `smoke-v030-webui-online-update-local.ps1` from a real v0.2.9.2 install;
  - until then, Windows artifact, receipt and 0.2.9.2 → 0.3.0 real upgrade remain blocked rather than falsely marked complete.

## S23 Controlled Skill Typed Process Protocol And Production Composition

- Implemented the product-owned `emate-controlled-skill-process-v1` contract. Its canonical `contract_id` binds Extension ID, exact revision/generation, normalized CAS tree digest, entrypoint path/digest, Python/Node runtime, interpreter digest, the manifest-v1 empty argument vector, declared environment keys, network domains and effects.
- Added the executable transport boundary behind a separate `ControlledSkillProcessBackend`: whole-CAS and entrypoint verification, interpreter re-hash, state fences before launch/during bounded waiting/after completion, canonical stdin/response identity, bounded stdout/stderr, timeout/cancellation termination and process-tree cleanup. No Skill content can supply a command or host argv.
- Added the only CAS host-path bridge, `resolve_verified_file`, which returns a path only after complete revision inventory and exact file digest verification.
- Connected the runner factory to production `compose_extension_service`. Production now exposes platform-specific readiness reasons instead of an absent adapter:
  - Windows: `windows_skill_cas_read_authority_unavailable`.
  - macOS: `macos_skill_file_read_scope_unavailable`.
- Production execution remains intentionally fail-closed. The current signed Windows security receipt accepts read roots only inside the immutable active slot, while durable `extension-cas` is outside it; its child protocol also accepts only exact Python `-I <artifact>`. The current macOS profile permits host-wide reads. Neither boundary can enforce the requested CAS/file declarations, and no generic shell/host subprocess fallback was introduced.
- Minimum native follow-up remains: extend the signed installer/helper receipt with a separately attested read-only durable CAS root and full tree digest, add typed Python/Node interpreter identities and argument schema, then replace host-wide macOS reads with exact runtime/CAS read roots. Per-domain networking still requires an egress broker; until then only networkless Skills can be admitted.
- Verification: typed transport, exact digest/protocol binding, production fail-closed reasons and executable source-boundary assertions passed `3/3`; Extension product composition passed `1/1`. The pre-existing shared-worktree `tests/v1/test_extension_execution.py:662` indentation error prevented collection of that unrelated module and was not modified in this slice.

## S24 macOS Universal And Online Publication Audit

- Audited active macOS staging, Runtime ZIP assembly, Mach-O signing, Candidate/promotion workflows, legacy v0.2.9.2 manifest bridge, current GitHub Actions configuration and public endpoints without dispatching or mutating anything.
- Active v1 staging is architecture-specific (`macos-arm64`, `macos-x64`) and performs deterministic ad-hoc signing only. It does not create the required `webui-macos-universal` package and has no Developer ID, hardened-runtime, notarization or stapling workflow.
- The atomic legacy manifest generator is correct in isolation but is not called by any active publish/promotion workflow. The retired macOS dispatch helper targets a nonexistent workflow and missing import script.
- GitHub workflow authority is present, but release readiness is not: stable signing/publication/deployment environments have zero secrets, stable signing has zero variables, no Apple credentials are referenced, the required self-hosted Windows runner is absent, and protected-ref enforcement is unavailable for this private repository under the current GitHub plan. The most recent platform-stage dispatch ended `startup_failure` with zero jobs.
- Public readback remains 0.2.9.2. A July 8 GitHub v0.3.0 macOS universal ZIP exists but is historical, not current-source evidence, and must not be promoted.
- Local evidence: legacy atomic pointer `2 passed`; update/publication `24 passed`; exact-byte/schema slice `1 passed, 1 skipped`; real macOS codesign `2 skipped` on Windows. ReleaseBuilder exposed one stale `0.9.0` delta fixture that must be changed to the real 0.2.9.2 baseline; Candidate suite timed out and is not counted as passing.
- Full blockers, CI state and exact release sequence: `docs/v0.3.0/macos-online-release-audit.md`.

## S25 Model cleanup, single Skill authority and hot generation refresh

- Removed GPT-5.5 from executable constants, model capability rules and current smoke/test fixtures. The sole retained literal is an intentionally old persisted row in the legacy administration migration test.
- Agent Skill discovery no longer reads or writes `skills_config.json`; that file is migration input only. Live query and mutation flow through the Runtime-bound ExtensionService authority.
- Scoped Skill tools now compare the batch Extension generation to the repository generation on every tool round. A newly installed/enabled Skill is discoverable without restarting the turn; old search/read facts fail with `skill_state_changed` after generation changes.
- Fixed the shared `test_extension_execution.py` indentation regression and added a same-turn hot-install discovery regression.
- Source/backend audit is now 7/7 achieved. No real production Usage/Audit reconciliation artifact was found; `usage-audit-production-gate.md` records the still-open external gate, exact read-only comparison command and report schema without inventing evidence.

## S26 Production Usage/Audit and Luna read-only acceptance attempt

- Correlated the local operator credential source with the previously redacted deployment receipt using only hashes: domain `A753D877497CBE35`, SSH host `CDF1CF905198CA97`.
- Current process has no Control Plane, Gateway/provider or OpenAI endpoint/bearer environment authority. Production read-only GETs reached the correct identity but returned `401` for admin Usage, admin models and Gateway models.
- Confirmed the draft public Usage route is not deployed: health, data and runtime-audit probes under `/ecorex-agent/usage/api/*` returned `404`. The source reverse-proxy contract exposes the Control Plane admin surface but not the loopback Usage service.
- Strict non-interactive SSH failed with exit `255`; only password credentials are available and the environment has no approved password-capable SSH transport. No password was printed, passed on a process command line or written to an artifact.
- Did not mislabel a metered model POST as read-only. Luna high needs either a separately authorized metered acceptance request or a protected server-side execution receipt. The current no-write slice cannot create that evidence.
- Result: production gate remains blocked on an approved short-lived read-only bearer or approved SSH channel; details and the corrected loopback/tunnel export command are in `usage-audit-production-gate.md`.

## S26 Final Independent Integration Gate

- Removed the last current-test references to the deleted `GPT_55` constant. Current model capability tests now exercise Luna; the only GPT-5.5 literal left in executable test data is the intentional persisted-row migration fixture.
- Added the real `task_list` handler and its fail-closed availability reason to the product composition regression expectation; this corrected stale test data, not Runtime behavior.
- Regenerated all Web Runtime contracts from the authoritative Python schemas after the Skill Hub/Extension projection changes.
- Independent results:
  - Model/catalog/capability: `65 passed`.
  - Usage projection and password/session revocation: `21 passed`.
  - Runtime composition/product app/legacy update: `31 passed, 1 skipped`, then the corrected product-app assertion `1 passed`.
  - Runtime kernel and Skill Hub registry/Runtime/transport: `12 passed`.
  - Cow seed lock validator: `release_gate=pass`, `53/53` terminal dispositions, zero network dependency.
  - Focused Ruff, generated-contract check and `git diff --check`: passed (only existing CRLF conversion warnings).
  - TypeScript: passed. Production Vite build: passed; 33 content-addressed assets; entry gzip `15.01 KiB`, initial JS gzip `149.98 KiB`.
- The real public manifest was read again and still returns `product=EcoreX`, `version=0.2.9.2`. No upload, signing or deployment was performed without release artifacts and credentials.
- Completion decision is recorded in `completion-audit.md`: local implementation is substantially complete, but the long Goal remains active behind production reconciliation, live Luna, two-platform package/upgrade, signing/upload/readback and final manifest publication gates.

## S27 Final GPT-5.5 Fixture Sweep

- A final repository-wide scan found the removed `GPT_55` constant still used by the obsolete `tests/test_models_handler.py` fixture. Migrated every current-model scenario and assertion in that file to `GPT_56_LUNA`, including its 400,000-token generic capability expectation.
- The historical handler suite itself is not an active runnable gate: it imports the removed `channel.web.web_channel` module and 20 cases fail at import before any model assertion. It was not deleted because it also records unrelated legacy behavior; the v0.3.0 model path is covered by the passing 65-test model/catalog/capability gate.
- After the sweep, GPT-5.5 remains only as the persisted-row migration fixture and explicit negative catalog assertions. There is no executable constant, active model policy, catalog entry or current positive test fixture.

## S28 Auditable Cow seed source mirror and canonical packages

- Reused the fixed upstream commit `0c214c3a61f66f8c122111c23270bd146241001b` and the original 53-entry size/SHA audit. The Cow download endpoint returns dynamic Registry URLs by default; requesting `{"mirror":true}` returns the R2 mirror bytes captured by the audit. All 53 original ZIPs were re-fetched, matched on both byte size and SHA-256, and persisted under the repository-only `source-packages` evidence directory (`7,470,533` bytes total). No user directory or server was modified and nothing was uploaded.
- Added deterministic `emate-declarative-canonical-v1` conversion. It accepts only one unambiguous root Skill, preserves the instruction body and every declarative resource, replaces legacy frontmatter only with bounded identity plus the locked catalog version/tags, and embeds `e-mate-provenance.json` containing original source digest, provider, catalog capture and upstream commit. ZIP timestamps, permissions, order and compression are deterministic.
- Result improved from zero mirrors to 28 CAS-verified canonical packages (`409,053` bytes), plus the existing five native aliases. The remaining 20 refusals are item-specific in `seed-canonicalization-audit.json`: 15 packages contain executable scripts without an auditable Runtime/effect contract; `60s-skills` and `feishu-tools` lack one unambiguous package root; `mckinsey-research` contains an HTML template outside the declarative allowlist; `reddit-insights` and `whatsapp-business` use non-scalar descriptions that cannot be normalized without choosing YAML semantics.
- The release validator now verifies every original source ZIP offline before considering any resolution. For mirrors it additionally verifies canonical ZIP size/SHA, CAS digest, locked version and embedded provenance fields. Source tampering, canonical package tampering, version drift, alias tampering, refusal-reason tampering and summary drift all fail closed.
- Verification: seed gate `5 passed`, including offline deterministic regeneration with dead HTTP/HTTPS proxies; focused Ruff passed. Final lock: `53` candidates = `5` native aliases + `28` mirrored + `20` explicitly unsupported, `0` pending, `network_dependency=false`.

## S29 Production reconciliation, deterministic frontend contracts and Luna safety gate

- Replaced the frontend contract command's machine-global Python dependency with `desktop/tools/run-python.mjs`. It prefers the repository `.venv` and is shared by package scripts and the code-generation test. `npm run contracts:check`, TypeScript, all `209` frontend tests and the production build pass.
- Added a credential-safe production operator helper that validates the expected domain and SSH host by digest, uses known-host rejection, keeps the password in memory and never emits credentials. Production Usage inspection found the loopback service was still running an older projection implementation.
- Atomically backed up and deployed the current authoritative Usage projection, restarted the exact service and retained automatic health/reconciliation rollback. The final Asia/Shanghai range `[2026-08-01, 2026-08-05)` passes exact Usage/Audit KPI equality with `projection_version=v0.3.0-usage-1`, `canonical_record_count=186`, `replaced_duplicate_count=0`, `unassociated_record_count=186` and `missing_provider_usage_count=0`.
- Production evidence is stored in `artifacts/usage-panel-production-deploy-v030.json`, `artifacts/usage-panel-production-deployment-inspection.json` and `artifacts/usage-audit-production-reconciliation.json`. No token fact was fabricated or historical record rewritten.
- A production Luna acceptance harness inspects the fixed production config paths over the same validated SSH channel and records only hashes/booleans/model identifiers. It found the live service still configured for `gpt-5.5` and an unauthenticated-public-IP-shaped provider origin using plaintext HTTP port 8080. The harness refused to transmit the bearer credential over that direct connection, so no metered request was made and Luna high remains an honest release gate.
- Corrected the signed ReleaseBuilder delta fixture to represent the legacy four-part `0.2.9.2` product baseline as valid SemVer `0.2.9+legacy.2`. ReleaseBuilder now passes `17 passed, 1 skipped`.
- Windows and macOS execution audits did not fabricate packages: Windows currently lacks a signed Bootstrap-compatible producer/receipt, and macOS lacks a universal Developer ID/notarized producer. The public 0.2.9.2 pointer remains unchanged.

## S30 Verified-background-update user experience

- Confirmed the authoritative Runtime updater already polls in the background, downloads into the staged transaction, verifies immutable identity, size, digest and signature, and only then enters `awaiting_user`. Activation continues through the existing Bootstrap supervisor, health confirmation and rollback authority.
- Corrected the WebUI presentation root cause: `available`, `downloading` and `failed` no longer create a dismissible product notification. The main banner appears only for a different target version in `awaiting_user` with `can_activate=true`, and explicitly says the package was downloaded and verified. Dismissing the banner persists only the presentation identity; it does not discard the prepared transaction.
- Settings remains the permanent manual entry. It now reports truthful idle, discovered, background download/verification, ready, activating and failed phases; duplicate manual checks are disabled while background preparation or activation is already in progress, while failed state remains retryable.
- Replaced the single fixed-delay page reload after activation with a deferred, bounded handoff. The old document waits through the Runtime restart window, polls authenticated Bootstrap until the target version is healthy (or observes the expected rotated-bearer 401), then uses `location.replace` with a version marker. This replaces the stale WebUI in the same tab and prevents browser Back from reopening it. A bounded timeout still replaces the page so Bootstrap recovery can present the surviving version.
- The handoff lives in its own dynamic chunk, preserving the initial bundle budget. Verification: focused update/Runtime tests `54 passed`; full frontend suite `214 passed`; TypeScript and generated contracts passed; production build passed with `34` content-addressed assets and initial JS gzip `149.98 KiB`.

## S31 WebUI-only distribution boundary

- User reconfirmed that v0.3.0 remains WebUI-only. No Electron, SwiftUI, `.app` product wrapper or native desktop interface may be introduced.
- Windows and macOS downloadable ZIPs remain local WebUI distributions: signed Bootstrap/Runtime background services, the signed React bundle, compatibility installer/launcher scripts, existing data directories and browser activation.
- `macos-universal` means one compatibility ZIP containing exact signed arm64 and x64 Candidate slots. The installer selects one by host architecture; it must not `lipo` already signed Candidate binaries into a new unbound executable.
- Apple notarization applies to the final ZIP and its exact signed executable payloads. ZIP distribution has no stapling claim. The accepted notarization response, Candidate identities and final package digest must be retained as release provenance.
- The legacy manifest now projects `health-gated-replace-existing-tab` for browser activation, matching the implemented WebUI handoff. Focused pointer tests remain `2 passed`.

## S32 Candidate-bound macOS universal WebUI producer

- Added an executable macOS producer and protected workflow contract for `EcoreX_0.3.0-webui-macos-universal.zip`. The output remains a browser WebUI ZIP: exact Candidate archives and signatures, the Candidate Core's signed React bundle, and an `Install e-Mate WebUI.command` architecture selector. The newly built `desktop/dist` is digest-checked against both signed Core slices but is not copied as a second unsigned bundle. No `.app`, Electron, SwiftUI or native product UI is introduced.
- The producer refuses raw platform-stage inputs. It re-verifies the complete Ed25519-signed stable Candidate, requires the exact macOS arm64/x64 Core, Bootstrap, capability-pack archives and pack manifests, and carries those immutable Candidate bytes into the compatibility ZIP. It does not `lipo` already signed Candidate executables into an unbound binary.
- The installer chooses arm64/x64 with `uname -m`, extracts only the matching signed Bootstrap archive to a private temporary directory, and invokes the Bootstrap `--local-release` path against the bundled signed Candidate directory. Existing machine product ID, data directory and browser WebUI lifecycle therefore stay under the existing Bootstrap installer authority.
- Apple distribution is fail-closed: both Candidate Bootstrap/Core slices must read back as the configured Developer ID Application identity with hardened runtime, the final ZIP must receive an `Accepted` `notarytool` result through the explicitly named keychain, and Gatekeeper must assess the signed launchers. A ZIP cannot carry a stapled ticket, so the distribution receipt records `stapling.applicable=false` instead of fabricating success.
- Added `emate.macos-distribution-receipt.v1`, binding source commit, Candidate release/build/manifest/receipt, every included macOS artifact hash and size, Developer ID requirements, Apple submission ID, Gatekeeper readback hashes, Web bundle digest and final ZIP digest/size.
- The final two-platform `emate.webui-build-receipt.v1 status=verified` is written only after strict verification of the Windows producer's `status=partial`, `production_eligible=true` receipt, exact Windows ZIP bytes and matching Candidate release/build/manifest provenance. A filename-only Windows handoff is rejected.
- All output is assembled under a sibling temporary directory. The final output directory appears through one atomic rename only after Candidate, Apple, Windows and receipt validation; any failure removes the temporary tree and leaves no package or verified receipt.
- CI contract downloads the exact protected stable Candidate and verified Windows WebUI artifact, builds the source-bound React WebUI, requires Apple signing/notary authorities, emits notarized package and receipts, and generates the legacy manifest through the existing atomic last-pointer function. It uploads only workflow evidence; it does not dispatch, publish or mutate the online manifest.
- Verification: producer syntax passed; focused macOS producer and legacy manifest tests `8 passed`. Fixtures prove non-macOS/missing authority failure, Accepted-notary receipt enforcement, Windows package tamper rejection, Candidate-only workflow wiring and atomic legacy manifest preservation.

## S33 Candidate-bound Windows x64 WebUI producer and offline Bootstrap path

- Added the missing Windows x64 compatibility producer. It accepts only a complete stable signed Candidate and its separately signed Candidate receipt, re-verifies the manifest and every artifact, and requires explicit production signing-key admission. Non-production fixtures are permanently marked `status=non-production` and cannot be promoted by the legacy manifest bridge.
- The package remains WebUI-only. It contains the exact signed Windows Core, Capability Packs, Bootstrap and Web manifest; it does not contain Electron, a native desktop UI or the retired `runtime/app.py`. The producer semantically verifies the Core's `runtime-config.json`, embedded signed Web manifest, React inventory and every static-file digest, then records the browser URL and Web bundle digest in `release.json`.
- Fixed the offline-install root cause in the signed Go Bootstrap. `--local-release <absolute-dir>` bypasses public discovery only after strict local verification: stable manifest signature and minimum version floor, current-host Core/required Packs/Bootstrap/Web manifest, size/SHA/artifact signatures, non-link paths and a closed directory inventory. Extra universal-package slices are allowed only when they are named by the same signed manifest and individually verify; unknown files and directories remain rejected.
- Aligned the Go Bootstrap anti-rollback sequence with `ecorex/product_version.py`. The obsolete v1-only parser would reject product version `0.3.0`; both sides now accept bounded final product SemVer and map `major/minor/patch` identically while still rejecting prerelease/build-decorated versions.
- `Install EcoreX WebUI.cmd` now invokes the packaged Bootstrap with the package-local `signed/release` directory, so the bundled Candidate bytes are actually installed before the existing Runtime service opens the React UI in the default browser. Existing EcoreX machine/data compatibility names remain unchanged while visible package metadata is e-Mate.
- Added independent `emate.windows-webui-build-receipt.v1`. Production output is only `status=partial`; the macOS/final aggregator now validates exact Windows package bytes, Candidate receipt, release/build/manifest identities, signing key, Core/Web identities and included inventory before it can write the existing two-platform `emate.webui-build-receipt.v1 status=verified` receipt.
- Verification: Windows producer, deterministic ZIP, production admission and Go offline source-contract tests `3 passed`; focused Ruff passed. The local host has no Go toolchain, so real Go compilation remains enforced by the existing pinned `setup-go 1.26.5` protected stage. No production artifact, fake signature, upload, deployment or public-manifest mutation was performed.

## S34 WebUI-only merged regression

- Re-ran the combined v0.3.0 implementation gates after the Windows/macOS producer work. Password/session revocation, Usage projection and reconciliation, Skill migration/Hub, automatic image routing, full-access permission bridging and the three update-package producers passed `44` Python tests.
- The Windows producer, macOS universal producer and legacy final-manifest bridge passed their focused merged gate with `11 passed`; Ruff, Python compilation and `git diff --check` passed.
- The React WebUI passed all `214` v1 tests. Generated Runtime contracts and TypeScript passed, and the production build emitted `34` content-addressed assets with initial JavaScript gzip `149.98 KiB`.
- No Candidate artifact is present locally, and this Windows host has no Go or Apple toolchain. Real signed Bootstrap compilation, Developer ID/notary execution, v0.2.9.2-to-v0.3.0 upgrades on Windows/macOS, live Luna high and public manifest publication remain release-environment gates; none was claimed or bypassed.

## S35 Browser tool authority and continuous-execution root fix

- Traced the reported “page script permission/tool entry unavailable” symptom through both execution paths. Legacy `AgentStreamExecutor` already retained `browser` as a core schema tool and delegated authorization to the verified Runtime permission snapshot; v1 WebUI instead obtains browser authority from the frozen capability plan, verified Browser Pack handler and just-in-time permission/availability overlay.
- Found the executable root mismatch in the shared v1 catalog. Browser Pack already implements `evaluate`, selector operations and same-page `batch`, but the model-visible `cdp` input schema declared `parameters` as an empty object with `additionalProperties=false`. Runtime therefore rejected every `expression`, `selector`, `text` and `steps` argument before the verified handler ran, making a present tool look unavailable and preventing continuous browser work.
- Aligned the existing catalog contract with the existing verified handler: bounded operation enums, script/selector/text/timeout fields and a bounded non-recursive batch-step schema. Added the missing Chinese alias `浏览器`, so an explicit Chinese browser request is promoted directly instead of requiring two avoidable discovery rounds. Browser Pack still owns public-network validation, full-access enforcement for `evaluate`, time/output bounds and process isolation.
- Removed the legacy tool-not-found path that read an arbitrary matching `SKILL.md` directly. A missing callable tool now reports only the authoritative tool set; Skill discovery/execution remains exclusively `skill_search → skill_read → skill_run`, preventing a second availability authority and raw-Shell fallback loops.
- Verification: full capability planner `106 passed`; focused browser/permission/pack regression `27 passed, 2 skipped`; missing-tool authority regression `2 passed`; Python compilation passed. Skips are platform-dependent browser staging cases on this Windows host, not failed behavior.

## S36 Local WebUI update must install before reopening

- Root-caused a false-success path in the packaged Bootstrap `--local-release` flow. When the current Bootstrap still owned `bootstrap-launch.lock`, the installer waited for the old WebUI and returned success; after acquiring the lock it also hot-opened any verified old Runtime and returned. Both branches skipped Candidate staging, slot installation and the 0.3.0 health launch.
- Local release installation now treats an occupied launch lock as an in-progress controlled restart: it waits up to five minutes for the old owner to exit, acquires the same exclusive lock, and then always continues through verified artifact staging, `install_local`, slot selection and supervised health launch. The ordinary `--launch-installed` and network-launch paths retain their existing hot-open behavior.
- Added a Go behavior test proving a local installer blocks until the existing launch owner releases the lock, plus a Windows package contract asserting the local installer cannot call `openRunningRuntime` or return through `waitForRuntimeAndOpen` before installation.
- Verification on this host: Windows WebUI package suite `3 passed`; `git diff --check` passed for the changed files. The host has no Go toolchain, so the new Go behavior test was authored but not compiled or executed locally; protected Go CI remains required before release.

## S37 Empty-final response and continuous-execution root fix

- Traced the duplicated `抱歉，我暂时无法生成回复` report through both execution authorities. A provider `response.completed` with no visible assistant text was accepted as a successful terminal event; the legacy loop also used the current run's step number (`turn > 1`) as its only evidence of prior tool work. A new user turn beginning with `继续…` therefore lost that evidence, generated the same fallback within seconds, and legacy delivery could render the fallback both from `agent_end` and the final reply while still presenting the run as achieved.
- v1 Runtime now requires non-whitespace output before finalization. An empty continuation after tools gets one tool-free text recovery request; a second empty terminal fails with `empty_final_response_after_tools`, leaves the completed tool execution intact, and never transitions the Job/Turn to completed.
- Legacy AgentStream now recognizes continuation language only when the preceding turn actually contains tool results, forces a tool-free final explanation, emits `outcome=partial/failed` for empty terminals, and does not persist its synthetic failure text as a successful assistant conclusion.
- Removed the destructive message-format/overflow recovery path. Provider-sensitive tool protocol is compressed to bounded text while retaining the original user request and completed tool facts, retried once without tools, and kept in agent memory on failure. AgentBridge and ChatService persist facts produced before an exception; no automatic recovery path clears the conversation store. Explicit user `clear_history` behavior is unchanged.
- Verification: combined AgentTurnWorker/continuity/permission regression `45 passed`. This includes same-run empty-final recovery, repeated-empty failure without false completion, next-run `继续…` recovery, format-failure fact preservation and pre-persisted-user deduplication. Three stale fixtures were corrected to emit visible text and reflect direct image routing. Focused Ruff, Python compilation and `git diff --check` passed.

## S38 Private-repository release admission

- Root-caused a release-orchestration dead gate: the private GitHub repository cannot expose branch protection with its current plan, while platform staging, Candidate signing, publication and the macOS WebUI aggregator all required `github.ref_protected=true`. Every legitimate v0.3.0 dispatch would therefore be rejected before producing an update.
- Replaced that unavailable signal with an equally fail-closed immutable-source admission shared by the affected workflows: exact repository `zhangyifanjackson-dotcom/EcoreX`, exact `refs/heads/main`, manual dispatch, a lowercase 40-hex administrator variable `ECOREX_V030_RELEASE_COMMIT_SHA`, and exact equality between that pin and `github.sha`. Existing Candidate provenance, signatures, hashes, protected signing/publication environments and readback checks remain unchanged.
- The variable is intentionally not guessed from the dirty workstation. It must be set to the final reviewed commit after source is committed; an empty or different value keeps every release job closed. This changes no online state and remains WebUI-only.
- Verification: focused workflow contracts `4 passed`; changed workflow/test `git diff --check` passed.

## S39 Skill authority cutoff in the legacy Agent bridge

- Removed the remaining legacy prompt bypass that exposed each Skill's absolute `SKILL.md` and base-directory paths and instructed the model to read them with the generic file tool. That path ignored ExtensionService enablement, version binding and the required progressive-disclosure order.
- Skill prompt projections now contain only discovery metadata. The legacy prompt builder emits them only when the authoritative `skill_search`, `skill_read` and `skill_run` tools are all callable; otherwise it emits no Skill instructions. Generic file and Shell tools are explicitly forbidden as a Skill-state bypass.
- Verification: bypass regression plus full-access bridge `7 passed`; focused Ruff, Python compilation and `git diff --check` passed.

## S40 Final Windows package trust boundary

- Root-caused the remaining two-platform aggregation gap: the Windows producer authenticated its Candidate inputs but never reopened its completed compatibility ZIP, while the macOS aggregator trusted only the ZIP digest recorded by an unsigned partial receipt. A modified launcher, added file or differently projected valid Candidate could therefore reach final aggregation without an independent package-semantic check.
- Added one shared final-package verifier in `ecorex.release.windows_webui`. It rejects absolute/traversal/backslash paths, links, non-files, duplicate members and any extra or missing inventory; bounds unsigned JSON/projected Bootstrap members before decompression; checks ZIP integrity; re-verifies the stable manifest, production-key admission, exact signed artifact sizes/hashes/signatures, signed Candidate receipt, Bootstrap byte projection and trust config, Runtime/Web manifest binding and every React asset digest; and requires the exact WebUI-only `release.json`, installer and README contracts.
- The Windows producer now runs that verifier after writing the ZIP and before emitting its partial receipt. The macOS producer runs the same verifier with its independently loaded production public key before consulting the partial receipt, and requires the embedded Candidate/manifest/release identities to match the exact Candidate being aggregated.
- Verification: focused Windows/macOS producer regression `10 passed`; Python compilation passed. Tests cover valid final-package reopening, extra/traversal/duplicate inventory and WebUI release-contract tampering. No package was published and no online state changed.

## S41 Windows WebUI producer workflow

- Added the missing Windows x64 workflow handoff required by the macOS final aggregator. It accepts only a manual dispatch from the exact repository/main/final pinned commit, downloads the fixed stable Candidate artifact, requires its signed receipt to bind the same commit, admits only the configured production Ed25519 public key, and invokes the existing WebUI-only package producer.
- The producer's new independent ZIP verifier must complete before the workflow uploads the fixed `emate-v030-windows-webui` artifact. The macOS workflow no longer accepts a caller-selected artifact name; it downloads only that fixed handoff and independently reopens it before final aggregation.
- The workflow installs no product UI and builds no desktop shell. Its only output is the Windows Runtime/Bootstrap/React WebUI compatibility ZIP plus partial receipt.
- Verification: Windows/macOS producer workflow and tamper contracts `6 passed`; `git diff --check` passed. No workflow was dispatched and no online state changed.

## S42 Authoritative home task activity

- Root-caused the home KPI drift: the WebUI grouped browser-local `thread.updated_at` values and inspected only each Thread's latest Turn. Multiple Turns in one Thread were collapsed, nonterminal states were reduced to one active Thread, superseded terminals were omitted, and a browser timezone different from Asia/Shanghai moved work across days.
- Extended the existing Runtime Usage projection with device-local task activity derived directly from current `turns.status` and terminal `turns.updated_at`. The projection returns seven Asia/Shanghai calendar days, today's completed/all-terminal counts, and the current count of every nonterminal Turn. Account-scoped Gateway Token replacement preserves this local task projection, while Token totals continue to use the existing unified Usage source.
- `HomeDashboard` now consumes only that Runtime activity for completed, waiting, success rate and the seven-day terminal trend. Thread projections remain used only for the recent-task list.
- Verification: Usage projection/API regression `6 passed`; full WebUI regression `215 passed`; TypeScript/contracts check passed.

## S43 Bootstrap Go execution gate

- Downloaded the workflow-pinned official Go 1.26.5 Windows amd64 toolchain into a temporary directory only. The archive was executed only after its SHA-256 matched the Go project's published value `97e6b2a833b6d89f9ff17d25419ac0a7e3b482a044e9ab18cdef834bd834fd38`; an earlier interrupted partial download was rejected by that check.
- Ran the complete `platform-staging/bootstrap` Go test package with that isolated toolchain. Result: `ok ecorex.local/bootstrap`, including the new local-release lock-wait behavior test. No system Go installation or global PATH change was made.

## S44 Fixed-upstream Astro Skill Hub WebUI

- Restored the explicitly requested Astro Hub surface from the fixed Cow Skill Hub page/card hierarchy at upstream commit `0c214c3a61f66f8c122111c23270bd146241001b`, with the existing MIT NOTICE retained. Visible branding is exclusively e-Mate; upstream provenance remains in source comments/NOTICE and card detail traceability rather than global page chrome.
- The Astro output is a static signed WebUI page at `/ecorex-agent/skills/`. It reuses the existing Runtime bearer injection, bootstrap CSRF and `/api/v1/skill-hub/*` authority for real search, classification, detail, download, upload and install. It adds no Cow OAuth, MySQL, D1, R2, Node service, Electron or native desktop shell.
- Reused the existing Vite production bundle authority: Astro output is merged before content addressing, its HTML and asset references are rehashed with the React bundle, and Runtime serves the verified embedded template with a fresh CSP nonce. Astro `4.16.19`, Vite `7.2.7` and the React plugin `5.1.1` are exact lockfile pins.
- Moved the strict Usage/task-activity wire validator into an on-demand chunk rather than raising the initial JavaScript budget. The Astro contract is part of the default frontend gate. Verification: WebUI `216 passed`, TypeScript/contracts passed, same-origin signed-route backend `1 passed`, and production build passed with `38` content-addressed assets; initial JavaScript gzip is `149.61 KiB` under the unchanged `150 KiB` gate.

## S45 Post-root-fix merged regression

- Re-ran the merged backend gate after continuous-execution, browser authority, task KPI, Skill authority/Hub and final update-package trust changes: `189 passed` in `150.20s`. The only warnings are Starlette's upstream multipart deprecation and the intentional duplicate-ZIP-member tamper fixture.
- WebUI remains `216 passed` including the Astro Hub contract; generated contracts and TypeScript pass; production build passes with initial JavaScript gzip `149.61 KiB`. The complete Go Bootstrap package also passes under checksum-verified Go 1.26.5.

## S46 Explicitly authorized Luna HTTP acceptance

- The user confirmed the deployed Luna HTTP BaseURL is a previously verified trusted endpoint. The production acceptance script now has an explicit `--allow-http-provider` switch; HTTP remains rejected by default and run mode still requires the exact 16-hex provider host digest.
- Acceptance evidence records `provider_transport_authorization=explicit-http` and separately reports HTTPS validity and transport authorization. Userinfo, query, fragment and unexpected provider hosts remain rejected.
- Focused regression: `2 passed`. A real metered request using the user-provided current provider configuration returned HTTP `200`, reported `gpt-5.6-luna`, honored `reasoning_effort=high`, and supplied provider usage (`4,396` total tokens). The evidence stores only the provider host digest, response ID digest and usage; no key or raw BaseURL is retained.

## S47 Cross-platform WebUI publication chain

- Root-caused the final public-update gap: the Windows and notarized macOS workflows stopped after uploading a GitHub Actions artifact. No production authority copied those exact bytes, read them back from the public download routes, or switched the v0.2.9.2 compatibility manifest.
- The macOS aggregator now exposes the verified workflow-artifact ID and digest to one existing production-deployment runner. That runner reopens the exact Actions archive with the shared extraction verifier, then invokes a server-local publisher under the existing product deployment lock.
- The publisher reuses the verified WebUI receipt and legacy-manifest projection. The workflow requires the existing protected `ECOREX_GITHUB_RELEASE_TOKEN`, publishes or byte-compares the unchanged filenames under `EcoreX-installers/v0.3.0`, and makes that release public before continuing. It then publishes both versioned ZIPs locally without overwrite and verifies size and SHA-256 through the unchanged `gh-proxy.com` primary plus `mvdcm.ecoremedia.net` and `dl.ecoremedia.net` fallbacks.
- Only after all three package sources agree does it atomically replace `legacy-pointer/manifest.json`. It reads the manifest back from both serving origins; a normal readback failure atomically restores the previous 0.2.9.2 pointer. Exact repository, `main`, and `ECOREX_V030_RELEASE_COMMIT_SHA == github.sha` admission remains fail-closed.
- Local verification: publication/rollback/workflow contracts `5 passed`; focused Ruff and `git diff --check` passed. No workflow was dispatched, no package was uploaded, and the public manifest was not changed.

## S48 Production controlled Skill Runner

- Replaced the production Skill Runner's permanent-unavailable composition with a typed adapter over the already verified Sandbox Capability Pack authority. The adapter accepts only the pack's digest-bound Python interpreter, the exact managed CAS revision, the configured workspace roots and the existing bounded controlled-process protocol. It does not construct a general host command and has no Shell/subprocess fallback; Node remains `missing_runtime` until a signed Node runtime is shipped.
- Product composition now obtains the sandbox authority from the verified `shell` pack handler and passes it to `ExtensionService`. Install/enable state and exact CAS hashing remain owned by `LocalSkillBundleStore` and the extension generation fences remain before launch, during execution and after completion.
- Added Windows AppContainer security receipt v4. Its only read authorities are the signed active-slot payload and the fixed product-owned `state/extension-cas`; the managed workspace remains the only write authority and network remains denied. CAS children are fully enumerated and ACL-attested, while mutable CAS inventory is not folded into the stable release receipt; the exact selected Skill tree is separately SHA-256 verified immediately before and after execution. Slot cleanup does not revoke the shared CAS grant, and permission-domain cleanup removes only its exact Package SID grant.
- Added a Skill-specific macOS Seatbelt policy with precise runtime, selected CAS revision, workspace and required system-library reads. The general workspace shell retains its prior profile, but controlled Skill execution no longer receives host-wide file reads. The behavioral probe now requires outside-host reads to fail in this scoped mode.
- Verification on this host: the largest combined controlled Runner/server/platform run passed `22 passed, 1 skipped`; an additional Windows security-contract/pack composition run passed `9 passed`. Focused Ruff, Python compilation and `git diff --check` passed. A real Python Skill completed through the attested sandbox adapter and exact per-revision read root in the test harness. The skipped case is platform-conditioned; this workstation has no MSVC or macOS signing/runtime environment, so native AppContainer compilation/execution and real `sandbox-exec` execution remain mandatory protected Windows/macOS Candidate gates before release.

## S49 Final local user and release verification

- Exercised the compiled WebUI as a real authenticated user in the in-app browser at desktop and `390 x 844` viewports. Dark/light five-robot home, retained Composer and Settings, immediate thinking state, streamed completion, Luna-high/full-access selectors, password form, manual update entry and Capability Center discovery/detail/install/enable all worked. The browser session was closed after verification.
- Browser testing found two shared GA projection defects rather than UI-only symptoms: Thread rows omitted `last_turn_status`, and the Skill Hub mock exposed list-only data. The GA authority now derives the latest Turn status and implements stateful detail/install projections. Its contract passes `10/10`.
- The merged backend run initially exposed two production-root defects. Legacy Skill migration attempted to ingest cache bytecode; the shared bundle reader now safely prunes cache directories and ignores validated `.pyc/.pyo` files. Windows sandbox receipts bound a random staging slot name; slot-contained roots now use stable `slot/...` identities while CAS/workspace roots remain install-relative. The final selected backend gate passed `242 passed, 2 skipped`.
- Final WebUI gates passed: `216` tests, generated-contract check, TypeScript and production build. The build emitted `38` content-addressed assets and kept initial JavaScript gzip at `149.61 KiB`. The checksum-verified Go `1.26.5` Bootstrap package passed `go test ./...`.
- Restored the local release virtual environment to the repository's exact hashed Runtime lock after the supply-chain gate correctly detected seven newer workstation packages. Added one exact reviewed MIT override for `zod-to-ts 1.2.0`, whose installed license file exists but whose lock metadata omits the field. Release/package/manifest contracts passed `59`; management/download/version/Runtime contracts passed `29`; supply-chain preflight passed with `24` locked Runtime packages and `599` production files scanned for secrets.
- `git diff --check` passed. The only full-changed-file secret detector matches are deliberate fake security fixtures in two tests; the production preflight reports no secret match. Luna evidence contains only hashes, status, model, reasoning and metered usage fields. The supplied provider key file remains outside the repository and was never copied into source or evidence.
- Repository-wide historical documentation still contains old GPT-5.5 records by design; active product/catalog/current fixtures contain only the explicit negative guards and one read-only legacy migration fixture. Public compatibility identifiers and URLs remain EcoreX as required, while visible WebUI/download/admin branding is e-Mate. Product and Runtime version are both `0.3.0`; no `1.0.18` release exists.
- Broad legacy suites were also sampled but are not claimed as complete: the repository-level suite still has a pre-existing missing `scripts/validate-ecorex-release-artifacts.py` collector dependency, and two larger runs exceeded their bounded local timeouts without reporting a failure. The focused release and product gates above are the authoritative local evidence.

## S50 Post-push production release readiness audit

- Confirmed `main`, `origin/main` and repository variable `ECOREX_V030_RELEASE_COMMIT_SHA` all point to reviewed commit `76620f90c908db2f3b44ba6298486f5a22b227f4` before attempting any release action.
- Read back both public compatibility origins. `mvdcm.ecoremedia.net` and `dl.ecoremedia.net` return identical `17,771`-byte manifests at version `0.2.9.2`; no package, release or pointer was mutated.
- A manual ordinary-CI dispatch created run `30931164996` but terminated as `startup_failure` before GitHub created any Job. The CI workflow bytes are identical to the last known successful run's source commit, all referenced Actions are GitHub-owned and SHA-pinned, GitHub recognizes all seven workflows as active, and actionlint found no structural/expression errors. This proves the startup failure is repository/account Actions admission state rather than the v0.3.0 source or workflow YAML. The GitHub REST API does not expose the account-level message.
- Repository and all eleven release environments have zero Secrets. Only `ecorex-release-stage` has the existing public/runtime configuration variables. The stable signer, publication, deployment and Apple notarization authorities required by the fail-closed workflows are absent.
- GitHub reports one online self-hosted Linux ARM64 Runner with the cloud/sign/publication/deployment labels. There is no registered Windows, macOS or live-acceptance Runner. The production server's actual Runner service was inspected over the existing known-host, password-in-memory operator channel; its process has none of the release signer, release-token or Apple-notary authorities, so the empty GitHub environment state is not hiding a reusable server-local signer.
- A hosted Windows/macOS build, trusted Candidate signature, Apple Developer ID/notarization, cross-repository installer publication and real two-platform upgrade cannot be truthfully executed from the current authorities. No substitute key, unsigned package, Windows-only pointer or manual hash bypass was created. Required external actions are: restore GitHub Actions hosted-job admission, provide the existing trusted signer configuration, configure Apple distribution credentials, register/provide the missing platform execution authority and supply a scoped installer-publication credential.

## S51 Retained-candidate Windows upgrade and launch root fixes

- Reused one retained local signed Windows candidate and the read-only 1.38 GB v0.2.9.2 installation snapshot; no platform rebuild or second legacy copy was performed. The real legacy inventory contained `20,372` files and `1,384,401,299` bytes. Windows identity checks now compare stable file-object identity instead of Unix mode bits, long snapshot paths use the native extended-path form, and release evidence follows the strict installed `state/current-runtime.txt → runtime-<version>/runtime-manifest.json` authority.
- Fixed two migration retry roots found by that real upgrade: the precreated empty `state/extension-cas` is preserved as product-owned state while any non-empty unowned CAS remains rejected; an already verified prepared import can join a retried install transaction without being downgraded to `dry_run_verified` or losing its exact transaction binding. The retained activation reached `bootstrap_health_confirmed`, committed the migration and selected the new slot without touching the user's actual install or desktop.
- The first full Runtime launch exposed `ModuleNotFoundError: common`. Runtime API registration imported the legacy permission broker, but the signed Core intentionally packages only `ecorex`. Moved the process-local verified-permission projection into `ecorex.permission_bridge`; the legacy broker imports that projection, while packaged Runtime no longer depends on `common` or its payload-local logger. The Pack Python probe now imports the bridge, and a Runtime composition regression rejects any reintroduced `common` dependency.
- Ran the fixed source with the retained candidate's exact packaged CPython, dependencies, signed slot and migrated data. Signed slot verification passed, `ProductRuntimeComposition` completed, the server stayed healthy, `/api/version` returned `product=e-Mate, version=0.3.0`, and `/` returned the addressed e-Mate WebUI. The frozen pre-fix candidate was not rewritten or re-signed; final immutable package generation remains the existing release workflow's job.
- Rebranded the unchanged public terminal-download page with the approved five-robot asset and e-Mate surface while preserving its install command and URLs. Windows shortcut convergence now replaces the exact legacy shortcut in place, removes released duplicate URL/CMD launchers transactionally and retains rollback/uninstall restoration.
- Focused merged regression passed `241 passed, 14 skipped`; the two initial failures were solely the shallow checkout's missing pinned v0.2.9.2 tag and passed after fetching exact commit `b52999b07a753e103a993a4da9d3c83c3f366e71`. Signed-delta/no-full-target-download, shortcut replacement and public-download tests passed `9`; dependency locks, Ruff, Python compilation and `git diff --check` passed. No public manifest or production installation was changed.

## S52 Public repository and Actions billing diagnosis

- At the user's instruction, changed `zhangyifanjackson-dotcom/EcoreX` from private to public. The separate installer repository was already public. The repository homepage now uses the visible e-Mate v0.3.0 brand while retaining the existing machine repository name and compatibility URLs.
- Before changing visibility, scanned all `2,515` tracked files with the release secret-shape detector and confirmed the supplied desktop model-key file is not tracked. The only matches are explicit redaction/supply-chain fixtures; no credential file was added or exposed by this change.
- Dispatched ordinary CI run `30978342014` from exact `main` commit `5fe2ae0eeabb430ed4ed55102565de12977c50cc`. Unlike the earlier zero-Job startup failures, GitHub created all four hosted jobs, then rejected each before runner assignment with the exact annotation `The job was not started because your account is locked due to a billing issue.` Making the repository public therefore proved the remaining admission failure is the account billing lock, not private Actions minutes, workflow syntax or source.
- Both public compatibility manifests were read back unchanged at `0.2.9.2`. No package, npm identity, production pointer or user installation was mutated; signed 0.3.0 publication still waits for billing restoration and the existing signer/Apple/platform authorities.

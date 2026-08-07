# EcoreX v0.3.2 验收记录

日期：2026-08-06

## 范围验收

| 能力 | 结果 | 证据 |
|---|---|---|
| 流式顺序、delta 合批、断线续传 | 通过 | reducer/unit、slow reconnect Playwright |
| 服务端 timing 与运行中校准计时 | 通过 | protocol/kernel/replay tests、retry browser scenario |
| 工具状态与结果原子收敛 | 通过 | reducer 与 Runtime contract tests |
| turn 级过程折叠 | 通过 | `timelineTurns.test.ts` 与 reasoning/retry scenarios |
| 用户/助手消息和五种终态 | 通过 | Web unit、终态复制与响应式矩阵 |
| 72px 滚动追随与回底 | 通过 | 120-turn Playwright scenario |
| 长会话动态高度虚拟化 | 通过 | Virtuoso contract、120-turn browser scenario、bundle gate |
| 动态交互与历史回执 | 通过 | HITL、connector login/device/cancel/reauth/restart scenarios |
| 管理端只读 gate 与显式 rollout | 通过 | admin Playwright 与 Control Plane tests |
| Skill/MCP 与工具 schema 权威 | 通过 | Skill workspace、MCP OAuth、generated schema/static authority gates |
| 图片/修图稳定性与并发边界 | 通过（本地合同） | artifact cache、retouch、image orchestrator 全量测试；真实共享存储压力仍由受保护 CI 执行 |
| 构建、包体、依赖与供应链 | 通过 | build/bundle/audit/lock/supply-chain/reproducibility gates |

## 自动化结果

- Python 全量：`2647 passed, 55 skipped`，361.21s。
- 显式发布与协议：`44 passed`。
- 迁移与 schema：`95 passed, 1 skipped`。
- Web unit/contract：`222 passed`。
- Chromium E2E：`51 passed`，最终全量结果写入 `.candidate/quality/playwright-v032-full.log`。
- 响应式与可访问性：1440、1024、768、390、320 宽度的 light/dark axe 矩阵通过；另含 forced-colors、reduced-motion、touch 与键盘焦点。
- npm audit：0 vulnerabilities。
- Python source/lint/compile、runtime/server schema authority、design system、legacy cutoff、public download site、dependency locks：通过。
- 供应链：许可证、运行时锁摘要与 secret scan 通过。
- 固定 v0.3.0 迁移基线：6372 个 Git 对象完整并通过校验。
- Web 可复现性：测试前后两份 byte contract 完全一致。

## 包体

- entry：25.93 KiB，gzip 8.64 KiB。
- initial JS：456.13 KiB，gzip 144.99 KiB。
- deferred features：206.71 KiB，gzip 72.42 KiB。
- JavaScript chunks：33；生产 Web assets：39。
- 新增运行时依赖仅 `react-virtuoso@4.18.11`；`requests` 不进入 runtime profile。

## 企业环境只读验收

- 服务器 SSH、磁盘、内存、Docker、systemd 与本机健康端点已只读检查。
- 根卷使用率 25%，约 7.3 GiB 内存、4 CPU；核心健康端点 `18771`–`18774` 均返回 200/ready。
- `ecorex-admin-api`、control-plane、gateway、image-api、image-worker 与 web 服务均 active/running；针对一次瞬时不可达，`image-api` 随后连续 10 次返回 200/ready，服务重启计数为 0。
- 当前 cloud symlink 仍指向已部署的 v0.3.0 release；下载区存在 v0.3.1 发布记录。
- 未读取远端环境变量或密钥，未修改服务、数据库、容器、发布指针或下载站。

## 发布边界

### Windows 兼容构建授权

2026-08-06，用户在确认严格的自托管 Windows runner 不可用后，明确授权
受保护 platform-stage 改用 GitHub `windows-2022` 兼容构建。该路径必须把
`authority_mode` 记录为 `github-hosted-ci-compatibility`，并继续校验 GitHub
Actions/Windows 2022 边界、MSVC 14.44 与 SDK 布局、Microsoft Authenticode、
源码与工具链清单摘要、观测到的工具/库摘要和最终二进制摘要；不得表述为
`caller-pinned` 严格工具链证据。

生产晋级受以下既有门禁保护，不能用本地未提交工作树绕过：

1. 精确 commit 的 CI 与 platform-stage 成功记录；
2. 真实 PostgreSQL/MinIO 256-job 双节点并发 gate；
3. 四小时 image soak；
4. 跨 runner 字节可复现证据；
5. 候选签名、Control Plane rollout 与健康切换。

因此本记录把“流程与候选可部署性”判为通过，但不会把未经提交、签名和受保护 CI 的本地代码直接覆盖企业生产。生产发布需要显式授权提交/推送，并由受保护 workflow 生成不可变候选后再晋级。

## 2026-08-07 手动生产发布

用户随后明确授权跳过 CI，改用手动构建、签名和生产部署。发布输入固定到
提交 `9cb691050ee893e305c6b6ab2d5a27766e424980`，云端不可变 release 为
`ecorex-cloud-v0.3.2-9cb691050ee8-manual`，Web release 为
`20260807083500-v0.3.2-9cb6910`。

- npm 官方 registry 查询成功；`npm ci` 下载 385 个包并完成 esbuild
  postinstall，`npm audit --audit-level=high` 为 0 vulnerabilities。
- Web typecheck、生产构建和 222 项 unit/contract 测试通过；实际
  `npm exec --call` 命令在 Node 22.23.1 / npm 10.9.8 下执行成功。
- 云端 schema/production contract gate 通过，green 槽位四个服务均为
  active，`18871`–`18874` 的 `/health/ready` 均返回 200/ready。
- activation journal 已清除，`/opt/ecorex/cloud/current` 原子切换到上述
  v0.3.2 release；Nginx 配置检查通过。
- `/opt/ecorex-web/current` 原子切换到上述 Web release；公网 Web 入口、
  content-addressed JavaScript asset 和控制面 readiness 均返回 HTTP 200。
- 迁移期间发现并处理两项既有生产漂移：旧 DeepSeek 上游 ID 与 v0.3.2
  托管策略不一致，以及公开 bootstrap pointer 的 freshness 签名损坏。
  原文件均保留备份；pointer 从数据库中的最近可信 preparation 恢复后，
  由现有 publication signer 重新刷新，未修改公开客户端信任链。

手动 release 使用一次性内存 Ed25519 发布密钥，仅用于本次服务器侧 artifact
验签；私钥未落盘，也未加入公开客户端 trust chain。公开下载指针继续保留官方
发布链，等待后续受保护发布流程生成正式跨平台安装包。

## 2026-08-07 安装与跨版本更新修复

- 生产下载入口撤回了错误的纯网页快捷方式，恢复为无 npm 依赖的原生
  PowerShell/macOS shell 安装命令；命令预建 `state/extension-cas`，避免旧
  Bootstrap 在 Runtime 组合阶段失败。
- Bootstrap 现在会创建 `state/extension-cas`；Go 测试通过。Companion 修复了
  已提交桌面入口被删除后无法自修复的问题：缺失或不匹配的旧入口不再触发摘要
  异常，修复事务会重建产品入口且不覆盖用户拥有的同名文件。
- 本机错误 `e-Mate.webloc` 已移到废纸篓，签名安装槽重建出
  `e-Mate.app`；`http://127.0.0.1:8765/` 与 `/api/version` 均返回 200，
  更新状态已回到 `idle`。
- 误生成的 1.0.7 云 release 与下载 artifact 已从生产删除；没有运行进程、
  systemd、Nginx、Git tag 或当前指针依赖它。生产 Cloud/Web/下载指针仍分别为
  v0.3.2、v0.3.2 和兼容安装 hotfix，四个云服务均返回 ready。
- 新增 0.3.2→1.0.0 跨版本门禁：Windows x64、macOS arm64、macOS x64 均
  验证下载、激活、重启请求和新版本启动收敛；浏览器 handoff 等待 1.0.0
  Runtime 健康后使用 `location.replace` 打开最新版页面。
- 聚焦回归：Python `39 passed, 8 skipped`；浏览器 handoff Node test 通过；
  Bootstrap Go tests 通过；公开下载站 `5 passed, 1 skipped`。

1.0.0 当前仍是待构建、待签名、待发布目标。跨版本在线更新只有在 1.0.0
沿用/轮换为 0.3.2 已信任的 release/publication key、三个目标安装包齐备并且
公开指针原子晋级后才允许对外宣称可用。

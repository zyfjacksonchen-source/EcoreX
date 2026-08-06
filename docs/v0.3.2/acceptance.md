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

# Model Gateway v1.0 生产运行手册

## 1. 产品边界

`ecorex-gateway` 是云端托管模型网关，不是本地 Runtime 的一部分。Provider 凭据只能由 Gateway 进程的 `GatewaySecretProvider` 从 Vault、sidecar 或 workload identity 获取，不得进入 WebUI、本地 Runtime、请求 JSON、SQLite、访问日志或异常文本。默认的环境变量 SecretProvider 只是一个固定逻辑名的部署适配器，正式环境应注入短期 workload token 提供者。

v1 内置存储实现只支持：

- `sqlite-wal`；
- 单进程、单副本；
- 一个持久卷上的绝对数据库路径；
- 跨平台非阻塞进程锁。

这不是 HA 方案。配置 `replica_count != 1` 或其他 storage backend 会直接拒绝启动，不会悄悄回退到 SQLite。PostgreSQL/多活版需要另行实现类型化 Store 合同并重新进行一致性评审。

## 2. 发布与启动顺序

1. 在加密持久卷上准备 Gateway 数据库目录。
2. 注入公钥环、固定 Provider origin/model mapping 和 SecretProvider。
3. 只由发布 Job 执行 `ecorex-gateway schema migrate`。
4. 执行 `ecorex-gateway schema check`，它会做只读 schema/WAL 检查、验签配置检查和真实 Provider `/v1/models` 探测。
5. 启动 `ecorex-gateway serve`。`serve` 不会建表、修表或修复未知 schema；未迁移、指纹漂移或历史被篡改都会 fail-closed。
6. 负载均衡器先检查 `/health/live`，再以 `/health/ready` 决定是否导流。

进程收到终止信号时先关闭准入，然后等待已接收流在 `graceful_shutdown_seconds` 内结束。新请求返回 `503 + Retry-After: 1`；已落库事件仍可用原 `request_id` 重放。超时后先关闭 Provider socket 促使流收敛；如 ASGI 流仍未释放，进程锁保持到 OS 终止进程，不会在仍可写时让第二个实例启动。

## 3. 必需配置

| 变量 | 含义 |
|---|---|
| `ECOREX_GATEWAY_STORAGE_BACKEND=sqlite-wal` | v1 唯一内置存储实现 |
| `ECOREX_GATEWAY_REPLICA_COUNT=1` | 强制单副本 |
| `ECOREX_GATEWAY_DATABASE_PATH` | 加密持久卷上的绝对路径 |
| `ECOREX_GATEWAY_STORAGE_ENCRYPTION_AT_REST=true` | 部署层加密声明；必须有云平台证据 |
| `ECOREX_GATEWAY_MODEL_MAPPING_JSON` | v1 必须包含精确映射 `{"ecorex-chat":"gpt-5.6-sol"}`；与托管策略不一致时拒绝启动 |
| `ECOREX_GATEWAY_PROVIDER_ORIGIN` | 无 path/query/credential 的 HTTPS origin |
| `ECOREX_GATEWAY_PROVIDER_ALLOWED_ORIGINS_JSON` | 精确 origin 允许列表，必须包含上一项 |
| `ECOREX_GATEWAY_AUTH_ISSUER` / `...AUDIENCE` | 短期 access token 的签发者与受众 |
| `ECOREX_GATEWAY_AUTH_PUBLIC_KEYS_JSON` | `key_id -> canonical base64 Ed25519 public key` |
| `ECOREX_GATEWAY_PROVIDER_BEARER_TOKEN` | 默认 SecretProvider 的部署适配；此密钥读取路径在模型升级中保持不变，不得写入配置文件 |

启用管理员热配置时，另设
`ECOREX_GATEWAY_ADMIN_MANAGEMENT_ENABLED=true`、指向 Control Plane 同一管理
数据库的 `ECOREX_GATEWAY_ADMIN_MANAGEMENT_DATABASE_PATH`、固定 HTTPS preset
映射 `ECOREX_GATEWAY_MODEL_PROVIDER_ORIGINS_JSON`，并由 SecretProvider 提供
`ECOREX_GATEWAY_MODEL_CONFIG_ENCRYPTION_KEY_B64`。此模式下新请求按已测试的活动
revision 即时换模型/Key，已开始的流继续使用冻结 revision；详细上线和回退顺序见
`admin-management-runbook.md`。
可用 `ECOREX_GATEWAY_CHAT_HANDOFF_TTL_SECONDS` 配置 Chat Completions 工具交接
的有效期（300–86400 秒，默认 3600）。

动态模式的 origin key 不是 API 协议名，而是每个模型槽位的独立部署出口：
`ecorex_chat` / `deepseek_chat` / `gemini_chat` / `doubao_chat`（及图片服务的
`ecorex_image`）。活动 revision 仍冻结 `responses` 或
`openai_compatible_chat` 协议，Gateway 据此选择安全适配器，不允许管理
请求提供 URL。Chat Completions 流和非流响应都转换为统一
`GatewayEvent`；并行工具调用、未知工具、超限响应和提交后不确定结果
均 fail-closed，不会透明重试 POST。

Chat Completions 不具备 Responses `previous_response_id` 语义，因此 Gateway
使用同一 SQLite WAL 保存限界工具交接。模型尝试在请求发出前绑定
`request/thread/turn/tool_call/config_id/revision`；上游工具调用先写为
`pending`，仅在 `tool_call.requested` 与 Gateway request 终态同事务提交
后转为 `available`。续跑请求必须在发起任何新的外部 POST 前原子消费：
重启可恢复，但双消费、过期、账户/会话/模型 revision 漂移和哈希损坏均
fail-closed。已消费后的进程崩溃按目标 request 的不确定终态收敛，不会
再消费交接或重提 Provider。每账户同时最多保留 256 个未消费交接。

超时变量分为 connect、read 和 total deadline。`gateway_lease_seconds` 必须至少比 Provider total deadline 长 30 秒，保证服务自己在租约内完成终态事实。HTTP 客户端强制关闭 redirect、环境代理和响应压缩；Provider URL 不能来自请求或模型输出。

## 4. 令牌、账户和模型隔离

统一公共安全层 `ecorex.security.Ed25519AccessTokenVerifier.verify()` 完成一次验签、issuer/audience、`iat/nbf/exp`、最长存活时间和标识符边界检查，只返回 `VerifiedAccessClaims`：

- `subject/client_id/account_id/organization_id/roles`；
- `AccessEntitlements.allowed_model_ids`；
- `quota_period/request_limit/concurrent_request_limit`。

Gateway 必须同时满足“服务全局 model catalog”和“账户令牌 model allowlist”才允许请求。额度字段缺失、模型交集为空、签名错误或过期均返回 401，路由不会接触 raw claims。Image Orchestrator 应复用同一 verifier 的 typed entitlement，不得另写一套 JWT 解析。

## 5. 上游 Responses 协议与故障语义

本地 Runtime 请求 Gateway 的正式路径是 `POST /v1/responses`，`/api/v1/model/stream` 作为 v1 内部协议兼容别名。Runtime client 只允许这两个精确路径、HTTPS 443 和显式 host allowlist，即使注入的 HTTP client 开启了 redirect 也会在每次请求层强制关闭。

Gateway 对上游 Provider 只请求另一个固定 allowlisted origin 的 `POST /v1/responses`，使用 Gateway `request_id` 作为上游 `Idempotency-Key`，且不对可能已跨过 Provider 边界的 POST 做透明重试。SSE 按单行、单事件、事件数和总字节四层限界，只接受支持的 [Responses streaming events](https://platform.openai.com/docs/api-reference/responses-streaming/response/completed) v1 事件；上游报错和 SDK 异常统一脱敏。

`ecorex-chat` 是迁移稳定的本地 ID，其 v1.0.0 托管策略固定到上游 [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。Gateway 在每个 Responses 请求中强制投影 `reasoning: {"effort":"medium"}` 和 `context_management: [{"type":"compaction","compact_threshold":272000}]`。后者使用官方 [Responses server-side compaction](https://developers.openai.com/api/docs/guides/compaction#server-side-compaction) 合同：渲染后上下文跨过 272000 tokens 时由上游执行 compaction，不是 WebUI 显示值或 metadata 假标记。Runtime 请求、模型目录快照和 `model.requested` 事件均携带同一策略 ID/版本/阈值；任一层不一致均 fail-closed。

Runtime 的 `direct_tools` 不会原样转发。Gateway 只从冻结 `ToolSpec + CapabilityDecision` 投影出 Responses function 的 `name/description/parameters`，并再次确认 `eligible + direct + version` 一致。带命名空间或超长的 EcoreX tool ID 使用每请求的稳定映射名，回包时再映射回 canonical ID；Provider 伪造未暴露工具名会被拒绝。`deferred/hidden` 能力不会被悄悄提升为上游工具。

持久化规则：

- 请求指纹、额度准入和每个输出事件先落 SQLite，再向客户端发送；
- 相同 `request_id + account + payload` 只回放已落库事件，不再调 Provider，因此不重复占用额度；
- 相同 `request_id` 更换账户或 payload 返回 409；
- 进程在 Provider 调用中崩溃时，租约过期后持久化不可自动重试的 `gateway_execution_uncertain`，不会再调一次 Provider；网络断开、慢流超时和协议中断同样不会被转成第二次自动计费调用；
- Provider 产生工具调用时，适配器先读到上游 `response.completed`，再交付 `tool_call.requested`。该事件是本轮的可回放 handoff 终态，立即释放并发额度；
- 不支持并行工具调用，上游同一轮返回多个 tool call 时 fail-closed，防止 Runtime 只执行其中一个。

## 6. 监控与外部 GA 证据

建议监控：401/403/409/429/503 比率、active request 数、租约过期数、`gateway_execution_uncertain`数、Provider connect/read/total timeout、SSE protocol rejection、额度使用率、WAL/卷容量和健康检查延迟。日志禁止写入 Authorization、Provider 响应体、请求原文和数据库绝对路径。

本地单元/伪 Provider 测试不能代替以下 GA 证据：

- 真实 Provider 账户、模型和限额下的协议证据；
- 公网 TLS 证书链、TLS 版本/密码套件和证书轮换证据；
- 负载均衡器的超时、流式缓冲关闭、排空和客户端断线证据；
- 加密持久卷、WAL 一致性快照、恢复演练和 RPO/RTO 证据；
- 真实容器杀进程、节点重启、慢流、洪泛和长时间并发 soak 证据。

未收齐这些证据前，不得宣称 Gateway 已具备多副本 HA 或真实 Provider GA 级可用性。

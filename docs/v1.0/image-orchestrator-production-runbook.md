# Image Orchestrator v1.0 生产运行手册

## 1. 固定部署合同

`ecorex-image` 是 v1.0 唯一生产入口：

```text
ecorex-image schema migrate
ecorex-image schema check
ecorex-image serve
ecorex-image worker
ecorex-image all
```

- `serve`：只提供用户 API，不租用图片任务。
- `worker`：只执行图片任务，仍提供 `/health/live` 和 `/health/ready`。
- `all`：小规模部署可在一个有界进程中同时运行 API 和 worker。
- 正式多副本部署建议分开 `serve` 与 `worker`，各自调整副本数。
- 生产只允许 PostgreSQL 15+ 作为任务、租约、事件和用量事实源，只允许私有加密 S3 作为共享 CAS。SQLite 仅保留给 local/test，生产 CLI 会直接拒绝。
- `serve`/`worker`/`all` 启动路径只校验 schema，永不执行 DDL。DDL 只能由 `schema migrate` 显式执行。

## 2. 必需配置

| 分组 | 环境变量 |
|---|---|
| 进程 | `ECOREX_IMAGE_STORAGE_BACKEND=postgresql` 、`ECOREX_IMAGE_INSTANCE_ID` |
| PostgreSQL | `ECOREX_IMAGE_POSTGRES_DSN`、`ECOREX_IMAGE_POSTGRES_POOL_MIN/MAX`、`ECOREX_IMAGE_POSTGRES_POOL_TIMEOUT_SECONDS` |
| S3 | `ECOREX_IMAGE_S3_BUCKET`、`ECOREX_IMAGE_S3_PREFIX`、`ECOREX_IMAGE_S3_REGION`、`ECOREX_IMAGE_S3_ENCRYPTION`，可选 `ECOREX_IMAGE_S3_KMS_KEY_ID` 和 HTTPS `ECOREX_IMAGE_S3_ENDPOINT_URL` |
| 用户身份 | `ECOREX_IMAGE_AUTH_ISSUER`、`ECOREX_IMAGE_AUTH_AUDIENCE`、`ECOREX_IMAGE_AUTH_PUBLIC_KEYS_JSON` |
| 模型 | `ECOREX_IMAGE_MODEL_ALLOWLIST_JSON` |
| 托管 Provider | `ECOREX_IMAGE_PROVIDER_ID`、`ECOREX_IMAGE_PROVIDER_ORIGIN`、`ECOREX_IMAGE_PROVIDER_ALLOWED_ORIGINS_JSON` |
| Provider 凭证 | SecretProvider 逻辑名 `managed-provider-bearer`；环境回退实现只读 `ECOREX_IMAGE_PROVIDER_BEARER_TOKEN` |
| 资源 | `ECOREX_IMAGE_MAX_BYTES`、`ECOREX_IMAGE_WORKER_CONCURRENCY`、`ECOREX_IMAGE_WORKER_MEMORY_ENVELOPE_BYTES`、`ECOREX_IMAGE_API_BLOB_MEMORY_ENVELOPE_BYTES` |

启用管理员热配置时，另设
`ECOREX_IMAGE_ADMIN_MANAGEMENT_ENABLED=true`、指向 Control Plane 同一管理数据库
的 `ECOREX_IMAGE_ADMIN_MANAGEMENT_DATABASE_PATH`、仅包含
`openai_compatible_image` 的固定 HTTPS preset 映射
`ECOREX_IMAGE_MODEL_PROVIDER_ORIGINS_JSON`，并由 SecretProvider 提供
`ECOREX_IMAGE_MODEL_CONFIG_ENCRYPTION_KEY_B64`。每个任务在入队时冻结配置 revision，
所以后续换 Key/模型不会改变正在重试或恢复的任务；详细流程见
`admin-management-runbook.md`。

管理员热配置使用云端直连适配器，不会把 Key 或上游 origin 暴露给本地
Runtime。生成固定调用 `POST /v1/images/generations`；精修从共享 CAS 校验并
读取底图、最多 15 张附加图片和可选 PNG 遮罩，再调用 multipart
`POST /v1/images/edits`。所有输入加总不得超过 `ECOREX_IMAGE_MAX_BYTES`。
只接受有界内联 `b64_json`，不下载或跟随 Provider 返回的 URL。
精修遮罩在云端转换为与第一张底图完全同尺寸的 RGBA PNG；EcoreX 内部
“255 表示选中区域”的语义会转换为 Provider 所需的透明 alpha。带遮罩时，
非 PNG 底图在像素和内存上限内转为同尺寸 PNG，避免格式不匹配导致精修
漂移或上游拒绝。JPEG 会先应用 EXIF 方向再转换，保证竖拍图片的蒙版仍使用
用户看到的坐标。图像编解码依赖只存在于 `image-cloud` 制品，不进入本地
Runtime Core。
大图的内部 ROI 蒙版仍保持确定性的 ≤2048px/4,194,304 像素上限；云适配
边界会依据原始 edit surface 和结构化标注重新编译并逐字节核对该蒙版，再由
Provider 适配器最近邻还原到第一张底图。它不会要求有界 ROI 文件本身等于
4K 底图尺寸，也不会接受与标注、覆盖率或像素区域不一致的替换蒙版。
结构化精修任务同时从不可变 edit surface 冻结原图宽高，Provider 输出必须
保持该画幅；不支持的尺寸在出站前明确失败，禁止静默回落到 1024×1024。

`gpt-image-2` 自定义输出尺寸在出站前按官方约束验证：两边必须 16px 对齐，
最长边不超过 3840px，长短边比例不超过 3:1，总像素必须位于
655,360–8,294,400；解码后的 RGBA 像素预算也不得超过
`ECOREX_IMAGE_MAX_BYTES`。不满足时任务在本地明确失败，不能把已知非法或
可能导致进程内存越界的请求发送给 Provider。约束来源见 OpenAI 官方
[Image generation guide](https://developers.openai.com/api/docs/guides/image-generation#size-and-quality-options)。

任意 DSN、S3、Provider origin/allowlist、Provider 凭证、JWT 公钥环或模型白名单缺失时，进程 fail-closed。生产建议由 Vault sidecar 或 workload identity 实现 `ImageSecretProvider`；不要把凭证放到 CLI 参数、日志或发布清单。

## 3. 首次上线顺序

1. 创建 PostgreSQL 15+ 数据库和最小权限账号。迁移账号可执行 DDL，Runtime 账号不应拥有 DDL 权限。
2. 创建 S3 bucket，开启 `AES256` 或 `aws:kms` 默认加密，四项 Public Access Block 全开，bucket policy status 必须为非公开。
3. 配置托管 Provider 的唯一 HTTPS origin 和等值 allowlist；不允许 HTTP、重定向、环境代理或上游返回的任意下载 URL。
4. 用迁移账号执行 `ecorex-image schema migrate`。命令会在迁移后验证 PostgreSQL 物理指纹、S3 写读删探针、Provider 健康和 JWT 信任配置。
5. 换成 Runtime 账号，执行 `ecorex-image schema check`。
6. 先部署一个 `worker` 和一个 `serve`，确认 readiness 后再扩容。

## 4. 健康、排空与恢复

- `/health/live` 只表示进程生命周期已启动。
- `/health/ready` 定期实际检查 PostgreSQL 迁移 receipt、S3 私有/加密控制、Provider 健康；worker 模式还要求 supervisor 正在健康租用。
- 收到终止信号后，立即拒绝新 API 任务/新租约，当前 provider 调用最多等待 `ECOREX_IMAGE_GRACEFUL_SHUTDOWN_SECONDS`。
- 超时的 provider 调用会尝试 cancel，但不会伪造本地终态。租约过期后，其他 worker 将其恢复为 `retry_wait` 并优先 `recover`，不盲目重新 submit。
- 提交阶段的超时、断线或 5xx 一律视为“结果不确定”；429 视为未接受的限流；4xx/redirect 视为明确拒绝。
- 429 的 `Retry-After` 只接受标准 delta-seconds/HTTP-date，并被夹在
  1–3600 秒内；畸形值使用有 jitter 的指数退避。完成回应后的
  结果下载 429 仍必须 recover-first，不得当作“未接受”重发。
  任一 429 都会立即持久化 scope-wide fence，其到期时间不得早于
  Provider 建议、当前 cooldown policy 和已有 fence 三者的最大值。
  不要通过提高 breaker threshold 来规避该全局限流栅栏。
- 熔断冷却到期后只允许一个数据库租约绑定的 half-open 探针。
  生产的探针租约长度按两个 Provider timeout 窗口加 Job lease 计算，
  覆盖 submit/recover 后紧接的结果下载。
- CAS 结果上传、校验和重读期间 Worker 必须继续 heartbeat。如果
  `committing` 之前已经持久化 staged result+usage，重启优先完成本地
  原子提交，禁止再调 Provider。

## 5. 并发与容量

- PostgreSQL 使用 `FOR UPDATE SKIP LOCKED` 和 fencing token，多 worker 可并行租用，过期 worker 不能提交晚到结果。
- v1 的持久结果合同是“一个 job 对应一个 CAS 图片”。生产会拒绝 `count > 1`，客户端需为每张图提交独立幂等 job，从而保留逐图重试、取消、计费和围栏。
- 数据库中的全局、账户、模型和操作类型限额是所有副本共享的权威 backpressure；API 超过队列限额返回 429。
- 每个 worker 进程还有本地有界并发，必须满足：

  ```text
  # 托管 EcoreX Image Service 模式（原生二进制结果）
  worker_concurrency * max_image_bytes * 3 <= worker_memory_envelope_bytes

  # 管理员 OpenAI-compatible 直连模式（输入 + Base64 JSON 解码峰值）
  worker_concurrency * max_image_bytes * 6 <= worker_memory_envelope_bytes
  postgres_pool_max >= worker_concurrency + 4
  provider_max_connections >= provider_max_concurrency >= worker_concurrency
  s3_max_connections >= max(worker_concurrency, api_blob_slots)
  ```

- API 上传和下载共用同一个 blob 内存信号量，不能各自吃满预算。
- S3 blob 与 reference document 都使用条件写，删除先写 tombstone，防止并发新 owner 被误删。
- `queued/retry_wait` 达到 deadline 后由 submit/lease/reclaim 任一事务
  幂等转为 `failed/deadline_exceeded`。如果监控中 queued 最旧时间
  超过最大 deadline，应视为 scheduler/reclaimer 停止并立即报警。

## 6. 安全边界

- 用户 API 仅接受短期 Ed25519/EdDSA access JWT，issuer、audience、token lifetime 和账户声明均在服务端校验。
- `account_id` 只来自已验证 JWT，不从请求体接受；Provider 响应必须回显相同 account/job identity。
- 模型白名单在 application service 和 Provider adapter 双重校验。
- 托管 EcoreX Image Service 的控制 JSON 上限 128 KiB；管理员直连 Images API
  的 Base64 JSON 上限按 `4 * ceil(max_image_bytes / 3) + 64 KiB` 计算。两种
  模式的解码图片上限都由 `ECOREX_IMAGE_MAX_BYTES` 决定，MIME、文件签名和
  SHA-256 必须一致。
- Uvicorn access log 关闭，CLI 失败只输出错误类型，不输出 SDK 错误、DSN、URL、bearer 或 provider request ID。

## 7. GA 外部门禁（不得用 mock 代替）

本地单元/集成测试只证明程序合同，不证明生产环境。GA 前必须独立留存以下真实证据：

- 真实 PostgreSQL 15+ 多连接/多进程的迁移、catalog drift、`SKIP LOCKED`、租约围栏和断电恢复。
- 真实私有加密 S3 的多副本 CAS 竞态、大文件、断网、限流、checksum 和 tombstone 对账。
- 真实托管 Provider 的 submit/recover/cancel 不确定结果、429/5xx、慢流、大响应、计费去重和至少一次 24 小时 soak。
- 按实际容量对 `serve` 和 `worker` 独立扩容/缩容，验证排空期间无新租约、无重复计费、无丢失终态。

任一真实门禁未通过时，不得将 Image Orchestrator 标记为 GA-ready。

## 8. 本地故障注入快速复验

```text
python -m pytest -q tests/v1/test_image_concurrency_stability.py
python -m pytest -q tests/v1/test_openai_compatible_image_provider.py tests/v1/test_dynamic_image_model_configuration.py tests/v1/test_image_orchestrator.py tests/v1/test_image_orchestrator_production_storage.py tests/v1/test_image_orchestrator_production_runtime.py tests/v1/test_managed_image_integration.py tests/v1/test_image_sqlite_schema_manager.py tests/v1/test_image_concurrency_stability.py
```

第一条使用受控假 Provider/SQLite 故障注入，覆盖过期队列释放、
Retry-After 边界、单 half-open 探针、慢 CAS 续租和 staged commit
重启恢复。它是确定性回归，不能用来宣称真实 Provider 的计费
exactly-once 或 24 小时稳定性；这些仍由第 7 节外部门禁约束。

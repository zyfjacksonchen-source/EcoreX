# EcoreX v1.0 管理后台运行手册

## 1. 产品边界

管理后台只负责四类运营动作：用户、用量、托管模型和版本发布。页面不保存
业务真相，也不能绕过后端状态机。所有写入都经过鉴权、乐观并发版本、幂等
请求 ID、不可变审计记录和后端合同校验。

内置 v1 实现是单节点产品边界：Control Plane、Model Gateway 和 Image
Orchestrator 必须能访问同一个加密持久卷上的管理数据库。它不是多机共享
SQLite 或多活方案；需要把三个服务分散到不同主机时，必须先实现并评审远程
配置分发 Store，不能把 SQLite 放到普通网络共享目录。

## 2. 管理员角色

| 能力 | JWT 角色 |
| --- | --- |
| 用户创建、筛选、编辑、停用与额度调整 | `user_admin` 或 `platform_admin` |
| 模型草稿、连通测试和启用 | `model_admin` 或 `platform_admin` |
| Candidate、灰度、全量、暂停、终止与回滚 | `release_admin` |
| 审计读取 | `audit_admin` |

同一个后台登录令牌可以包含多个角色，但每个 API 仍独立校验最小角色。页面拿到
403 时必须显示无权限，不能把按钮成功状态留在本地。

## 3. 单一配置事实源

Control Plane 数据库是用户、用量和模型配置的唯一事实源。三个进程必须使用
同一份 32 字节 AES-256 密钥，但各自通过自己的 SecretProvider 逻辑名读取：

| 进程 | 开关与数据库 | 密钥环境适配 |
| --- | --- | --- |
| Control Plane | `ECOREX_CP_ADMIN_MANAGEMENT_ENABLED=true`；使用 `ECOREX_CP_DATABASE_PATH` | `ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64` |
| Model Gateway | `ECOREX_GATEWAY_ADMIN_MANAGEMENT_ENABLED=true`；`ECOREX_GATEWAY_ADMIN_MANAGEMENT_DATABASE_PATH` 指向同一文件 | `ECOREX_GATEWAY_MODEL_CONFIG_ENCRYPTION_KEY_B64` |
| Image Orchestrator | `ECOREX_IMAGE_ADMIN_MANAGEMENT_ENABLED=true`；`ECOREX_IMAGE_ADMIN_MANAGEMENT_DATABASE_PATH` 指向同一文件 | `ECOREX_IMAGE_MODEL_CONFIG_ENCRYPTION_KEY_B64` |

正式部署应由 Vault、KMS sidecar 或 workload identity 提供密钥。环境变量只是固定
逻辑名的部署适配，密钥不能进入 WebUI、发布清单、命令行、日志或备份说明。

Provider 地址仍由部署者固定允许，管理员只能选择预置接口类型，不能在页面输入
任意 URL：

```text
ECOREX_CP_MODEL_PROVIDER_ORIGINS_JSON=
  {"responses":"https://...","openai_compatible_chat":"https://...","openai_compatible_image":"https://..."}
ECOREX_GATEWAY_MODEL_PROVIDER_ORIGINS_JSON=
  {"responses":"https://...","openai_compatible_chat":"https://..."}
ECOREX_IMAGE_MODEL_PROVIDER_ORIGINS_JSON=
  {"openai_compatible_image":"https://..."}
```

值必须是无凭据、无 path/query/fragment、443 端口的 HTTPS origin。Control
Plane 的测试器、Gateway 和 Image Orchestrator 应使用一致的 preset → origin
映射。生产配置原有的模型 allowlist、鉴权、公钥、超时和资源上限仍必须存在；
打开动态管理不会放宽这些边界。

## 4. 模型更换闭环

1. 管理员选择固定的 EcoreX 模型位：主模型、DeepSeek、Gemini、豆包、生图或
   精修；填写页面名称、上游模型名、接口类型和新 API Key。
2. 保存只创建不可执行草稿。API Key 立即 AES-GCM 加密，页面和列表只返回短
   指纹，不允许读回明文。
3. 点击“测试并启用”。Control Plane 使用草稿的冻结 revision 对 allowlisted
   origin 做有界真实连通测试；失败只记录稳定错误码，不能激活。
4. 测试通过和默认模型切换在同一事务完成。新的聊天请求立即读取新 revision；
   已开始的流继续使用旧 revision，避免半路换 Key。
5. 生图/精修任务在入队时持久化 `config_id + revision + upstream_model_id`。
   重试、恢复和进程重启继续使用原 revision，不能因管理员随后换模型而重复
   计费或改变结果语义。
6. 旧 revision 只在没有活动引用后关闭连接。管理员无需改 Python、重启服务或
   重新打包 WebUI。

生图与精修的活动 revision 在云端 Image Orchestrator 内直接适配固定
OpenAI-compatible Images API：无输入走 `POST /v1/images/generations`，精修走
multipart `POST /v1/images/edits`。底图、参考图和遮罩只按 SHA-256 从私有共享
CAS 读取，API Key 不会下发本地 Runtime。上游必须返回内联 `b64_json`；禁止
跟随上游提供的图片 URL。一次提交遇到超时、断线、408/425 或 5xx 后可能已经
计费，因此只进入 `provider_uncertain`，恢复路径绝不盲目重提。429 才按明确
未接受处理并执行有界退避。

遮罩由云端统一适配，不要求 WebUI 理解 Provider 细节：内部灰度选区会按底图
尺寸做最近邻缩放并转换为 RGBA，选中区域转换成透明 alpha；带遮罩的非 PNG
底图会在严格像素/字节预算内转为匹配 PNG。管理员替换模型前应以同一局部选区
样例验证“选区内改变、选区外保持”以及返回尺寸，不能只以 HTTP 200 判定可用。

需要回退时，使用上一组已保管的模型名和 Key 新建一个 revision，再执行同样的
测试并启用。后台不会解密并展示历史 Key，也不会允许未测试的历史 revision 直接
恢复为活动状态。

## 5. 用户与用量

- 用户列表支持姓名/账号搜索、组织和状态筛选、分页，以及创建/编辑。
- Token 与图片额度属于用户 revision；并发编辑返回冲突，页面必须刷新后重试。
- 用量只能通过带原因的正负调整记录校正，不能覆盖累计值。账本、用户投影和审计
  在同一事务提交；重复 `client_request_id` 返回原结果。
- 停用用户不删除历史会话、用量或审计。

## 6. 发布与推送

发布页只操作已经过门禁并进入 Control Plane 的不可变 Candidate。管理员可以：

- 按用户/组织/比例创建灰度并显式激活；
- 选择“全量用户”，后端强制比例为 100%；
- 暂停、继续、终止灰度，或设置通道 Kill switch；
- 创建只指向已发布、曾真实灰度且平台兼容的回滚 Candidate。

创建灰度不等于发布。只有后端确认 Candidate 签名、平台矩阵、最低兼容版本和
通道状态后才会生成 `update.available`；客户端仍需用户点击“更新并刷新”才激活。

## 7. 上线与故障检查

1. 备份 Control Plane 数据库，执行 `ecorex-control-plane schema migrate` 和
   `schema check`。
2. 使用同一管理数据库与密钥分别执行 Gateway、Image 的 `schema check`；任一
   schema、密钥或 origin 不一致都应 fail-closed。
3. 启动服务后，先添加测试模型草稿，确认“测试并启用”通过，再做一次真实聊天、
   单张生图和局部精修。
4. 检查新请求使用新 revision，正在运行的旧请求不被中断，日志中没有 Key、请求
   正文或 Provider 原始错误。
5. 再执行目标用户灰度；未完成 Candidate 门禁时禁止全量推送。

常见失败：

- `provider_test_unavailable`：检查固定 origin、TLS、DNS 和出口策略；不要在页面
  临时改 URL。
- `provider_model_unavailable`：上游不存在该模型名，修正草稿后产生新 revision。
- 解密失败：先确认三个进程读取的是同一密钥和数据库；禁止自动生成新密钥覆盖。
- Gateway/Image 找不到活动配置：确认对应模型位已经“测试并启用”，用途与本地
  模型位匹配，且进程的静态 allowlist 仍允许该本地 ID。

## 8. 回归命令

```text
python -m pytest -q tests/v1/test_control_plane_management.py tests/v1/test_dynamic_image_model_configuration.py tests/v1/test_openai_compatible_image_provider.py tests/v1/test_control_plane_admin_web.py
cd desktop
npm run test:v1
npx playwright test e2e/admin-web.spec.ts
```

本地假 Provider 只证明合同、热切换和故障语义。正式发布仍需要 Candidate 绑定的
真实主模型、生图、精修和浏览器验收凭据。

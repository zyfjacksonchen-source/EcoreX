# v0.2.9.2 Admin 管理数据迁移

## 边界

`migrate-v0292-admin-management.py` 只读取 v0.2.9.2 发布版 Admin SQLite 的
四张表：`users`、`client_sessions`、`usage_events` 和
`model_credentials`。它不查询或复制 message、conversation、thread、
share 等表。源库以 SQLite `mode=ro + query_only` 打开，迁移前计算文件
SHA-256 和已读行的 canonical snapshot SHA-256。

导入规则：

- `deleted_at` 非空用户排除；`active` 保留，`disabled/suspended` 转为
  v1 `suspended`，`invited` 不导入。
- 只统计已导入用户的 usage；Token 优先使用 `total_tokens`，旧事件未记
  Token 时使用 `amount`。额度优先保留 weekly limit，否则使用 daily
  limit。
- v0.2.9.2 没有独立图片额度字段；迁移将已用图片数同时设为保守的
  初始上限，管理员可在 v1 后台根据新套餐调高，不会凭空赠送额度。
- session 凭据不导入；已撤销、已过期 session 只计入脱敏摘要。
- 全局且启用的 OpenAI / DeepSeek / Gemini / 豆包配置分别进入
  `ecorex-chat`、`ecorex-deepseek-v4-pro`、`ecorex-gemini-3.1-pro`、
  `ecorex-doubao-seed-2.0-pro`。主模型上游固定迁移为
  `gpt-5.6-sol`，API Key 保持不变。
- 旧库存在图片凭据时，同一密钥导入 `gpt-image-2` 与
  `gpt-image-2-edit`，上游 ID 统一为 `gpt-image-2`。
- 真实 v0.2.9.2 库常只有四个 chat config；若存在启用的全局 OpenAI
  主凭据但没有独立图片凭据，迁移会复用该 Key 自动创建两个
  `gpt-image-2` 草稿，确保图片 selector 不缺入口。显式图片凭据始终优先；
  多个显式凭据竞争同一槽位时整体 fail-closed。
- 模型凭据只在迁移进程内存中存在，用 v1 AES-256-GCM authority
  重新加密。迁移后是 `draft/not_tested`，管理员仍需执行一次真实
  “测试并启用”，旧版可用不等于 v1 测试已通过。

## 执行

先对已显式创建 schema 的空 v1 管理库 dry-run：

```powershell
python scripts/migrate-v0292-admin-management.py `
  --source C:\secure\v0292-admin.sqlite3 `
  --target C:\secure\v1-control-plane.sqlite3 `
  --dry-run
```

提交时密钥不得出现在 argv。默认从
`ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64` 读取 canonical base64 32-byte key：

```powershell
$env:ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64 = '<secret authority>'
python scripts/migrate-v0292-admin-management.py `
  --source C:\secure\v0292-admin.sqlite3 `
  --target C:\secure\v1-control-plane.sqlite3
```

也可用 `--encryption-key-stdin`由受控调用方通过 stdin 传入。命令只输出计数、
状态和哈希，不输出路径、用户内容、session commitment、模型 Key 或
Key fingerprint。

提交是一个 `BEGIN IMMEDIATE` 事务。目标库已有 user/model 业务数据、旧模型
槽位重复、schema 不匹配或源数据非法时整体回滚。同一 snapshot 的重复执行
通过 v1 idempotency ledger 返回 `already_imported=true`，不重新加密或插入。

## 验证

```powershell
python -m pytest -q tests/v1/test_legacy_admin_management_import.py
python -m pytest -q tests/v1/test_candidate_release_pipeline.py
```

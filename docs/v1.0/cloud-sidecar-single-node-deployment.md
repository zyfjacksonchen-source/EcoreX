# EcoreX v1 单机云服务 side-by-side 部署

## 结论与边界

本方案只覆盖 **Alibaba Cloud Linux 4、aarch64、单机单副本**：

- Control Plane：SQLite WAL，单进程；
- Model Gateway：独立 SQLite WAL，单进程；
- Image API/Worker：PostgreSQL 15；
- Share/Image CAS：私有、TLS、服务端加密的 S3；MinIO 只是候选实现；
- Python：运行平台与制品内解释器均固定为 `3.11.9`；
- systemd 管理服务，Nginx 只暴露 Control Plane 管理页面/API；Gateway、Image
  继续只监听 loopback；
- `blue/green` 保存两套不可变代码，数据库和对象存储仍是唯一事实源。

这不是多机 HA。SQLite 不放 NFS/SMB/普通共享盘，不同时启动两个 Control Plane
或两个 Gateway。Image API 与 Worker 通过 PostgreSQL 租约/围栏并发。

部署器是本机工具，不含 SSH、云 API 登录、包管理和密钥输入：

```text
python scripts/deploy-v1-cloud-sidecar.py --spec /etc/ecorex/cloud/deployment-spec.json
```

不带 `--apply`/`--rollback` 永远只输出 dry-run 计划。当前仓库没有执行任何远程
部署。

## 磁盘与加密硬前置

`/var/lib/ecorex` 必须位于一个已加密的持久卷。支持两种可审计证明：

1. `luks2`：卷由 LUKS2 打开后挂载；保留 header 备份、恢复密钥和变更审计；
2. `alibaba-cloud-kms`：加密云盘由 KMS 密钥保护；把不可变的云盘/密钥证明归档，
   在 attestation 中记录证据引用与证据文件 SHA-256。

部署器同时固定 attestation 文件自身 SHA-256。仅设置
`ECOREX_*_STORAGE_ENCRYPTION_AT_REST=true` 不构成证明；示例 attestation 故意是
`"encrypted": false`，复制后不能误过门禁。现网若没有加密卷证明，部署必须停在
`encryption_attestation_*`，不得直接切流。

推荐目录：

```text
/var/lib/ecorex/control-plane/       Control Plane DB/WAL、备份、Share spool
/var/lib/ecorex/gateway/             Gateway DB/WAL
/var/lib/ecorex/postgres/15/data/    PostgreSQL 15 数据
/var/lib/ecorex/minio/data/          MinIO 对象数据
/var/lib/ecorex/image/               Image 本地运行状态
```

审计正文已有应用层 AES/HMAC；磁盘加密保护 SQLite、PostgreSQL/WAL、备份和临时
文件。MinIO 还必须通过 KES/KMS 提供 bucket 默认 SSE-S3/SSE-KMS，不能用“底层盘
已加密”伪装 S3 API 的 `GetBucketEncryption` 结果。

## MinIO 兼容性是硬门禁

EcoreX 不以“兼容 S3”宣传或 `HEAD bucket` 成功作为上线依据。部署切流前依次执行：

```text
ecorex-control-plane schema migrate
ecorex-control-plane schema check
ecorex-gateway schema migrate
ecorex-gateway schema check
ecorex-image schema migrate
ecorex-image schema check
```

Control Plane/Image 的真实 check 会检查或执行：

- bucket 默认 `AES256`/`aws:kms` 加密；
-四项 Public Access Block；
- Image 还要求 `GetBucketPolicyStatus` 明确为非公开；
- 带 `If-None-Match: *`、SHA-256 checksum、SSE 头的条件写；
- 带 checksum 的 `HEAD/GET`、ETag、元数据、条件删除和写读删探针。

任一 MinIO 版本/API 返回不支持、忽略条件头、缺 checksum、缺 Public Access Block、
没有默认 SSE 或 TLS 校验失败，部署器返回
`control_plane_production_contract_failed`/`image_production_contract_failed`，停止候选
启动，Nginx 和旧 slot 保持不变。此时只能修正 MinIO/KES 配置或换成通过同一门禁的
私有加密 S3，不能放宽生产校验。

## 制品合同

上传目录固定在 `/srv/ecorex-upload/<release_id>`，目录必须包含：

```text
cloud-release-manifest.json
cloud-release-manifest.sig.json
venv/bin/python3.11
venv/bin/ecorex-control-plane
venv/bin/ecorex-gateway
venv/bin/ecorex-image
deployment/systemd/*.service
deployment/nginx/*.conf
```

manifest 固定 `version=1.0.0`、`platform=linux`、`architecture=aarch64`、
`python_version=3.11.9`，列出每个普通文件的长度和 SHA-256。签名是 canonical JSON
上的 Ed25519 签名；公钥环路径和公钥环文件 SHA-256 也由部署 spec 固定。制品不接受
符号链接、目录逃逸、重复路径、未知目标或任一字节漂移。

部署器把制品复制到：

```text
/opt/ecorex/cloud/releases/<release_id>
/opt/ecorex/cloud/slots/blue/current
/opt/ecorex/cloud/slots/green/current
/opt/ecorex/cloud/current
```

先复制到同文件系统临时目录、再次验签/逐文件验 hash，再原子 rename。正在使用的
release 目录不原地覆盖。

## 身份、配置与秘密

预创建无登录 shell 的 `ecorex-cloud`、`ecorex-storage` 用户。systemd 单元启用
`ProtectSystem=strict`、`NoNewPrivileges`、私有临时目录、设备/内核/控制组保护与
最小 `ReadWritePaths`。

非秘密配置来自 `/etc/ecorex/cloud/config/*.env`，秘密适配文件来自
`/etc/ecorex/cloud/secrets/*.secret.env`。秘密文件必须是普通文件、root 持有且
权限不宽于 `0600`。生产优先由 Vault/KMS sidecar 或 workload identity 生成短期
文件；部署器不接收 secret CLI 参数，不把环境、子进程 stdout/stderr、URL、DSN、
Provider request ID 写入输出。迁移通过 `runuser` 以 `ecorex-cloud` 身份执行，秘密
只在子进程环境内传递。

Control Plane、Gateway、Image 的模型配置加密密钥必须一致，且 Gateway/Image 指向
Control Plane 管理数据库。Provider origin 只可由固定部署 preset 提供，管理员页面
不能输入任意 origin。

## 首次准备（不由部署器代做）

1. 建立并验证加密持久卷，归档 attestation；
2. 安装精确 Python `3.11.9` 到
   `/opt/ecorex/platform/python-3.11.9/bin/python3.11`；
3. 安装 PostgreSQL 15，限制 loopback + TLS + SCRAM，迁移账号和 Runtime 账号分离；
4. 安装 digest-pinned MinIO、TLS 证书和 KES/KMS，创建私有 bucket；
5. 安装 Nginx，并在现有 TLS server 中 include
   `/etc/nginx/ecorex-cloud/ecorex-cloud.routes.conf`；
6. 创建目录/用户并放置 root-only 配置、密钥、公钥环；
7. 生成并签署 aarch64 云服务制品，上传到固定围栏目录。

模板位于 [deploy/ecorex-cloud-sidecar](../../deploy/ecorex-cloud-sidecar)。示例里的
`REPLACE_ME`、关闭的 encryption 和 signer/provider 占位都是上线阻断值；
正式配置必须按各生产 runbook 补全，不能作为默认生产值。

## Dry-run、部署与目标围栏

先执行 dry-run：

```text
python scripts/deploy-v1-cloud-sidecar.py \
  --spec /etc/ecorex/cloud/deployment-spec.json
```

只有全部 blocker 清零后，才允许在目标主机本地运行：

```text
python scripts/deploy-v1-cloud-sidecar.py \
  --spec /etc/ecorex/cloud/deployment-spec.json \
  --apply \
  --confirm-target <sha256-of-exact-/etc/machine-id>
```

部署器同时验证：OS `alinux` major 4、aarch64、root、本机 machine-id hash 与 spec/
确认参数三者完全相等、固定二进制路径、Python 3.11.9、PostgreSQL 15、MinIO、
systemd、Nginx、两个服务账号、PostgreSQL/MinIO systemd active。部署 spec 不能把
artifact/keyring/attestation/二进制重定向到围栏外。

## 切换、健康和自动回滚

端口固定：

| slot | Control Plane | Gateway | Image API |
|---|---:|---:|---:|
| blue | 18771 | 18772 | 18773 |
| green | 18871 | 18872 | 18873 |

单机 SQLite 不允许新旧 Control Plane/Gateway 同时写。因此切换顺序是：

1. 锁定 `/run/lock/ecorex-cloud-deploy.lock`；
2. 验签并 stage 新 release；
3. 安装签名覆盖内的 systemd/Nginx 模板，写 inactive slot 配置；
4. 以新代码执行 migrate + 完整 check；
5. 停止旧 slot，再启动候选 slot；
6. 三个 `/health/ready` 均 200，Image Worker systemd 已启动；
7. 原子替换 `active-control-plane.conf` symlink，`nginx -t` 后 reload；
8. fsync 写入 `/var/lib/ecorex/cloud-deploy/active.json` 和 `current` symlink。

候选 health 或 Nginx 失败时，部署器停止候选、重启旧 slot，并保持/恢复旧 Nginx
上游。明确回滚命令：

```text
python scripts/deploy-v1-cloud-sidecar.py \
  --spec /etc/ecorex/cloud/deployment-spec.json \
  --rollback \
  --confirm-target <same-machine-id-sha256>
```

回滚只切代码和流量，绝不自动降级 schema 或复制 SQLite 文件。旧代码必须先对当前
schema 执行 `schema check`；不兼容则返回 `rollback_schema_incompatible`，停止回滚，
按 runbook roll-forward。Control Plane 自己的 verified SQLite backup 是数据库恢复
唯一入口。

## 上线前仍需真实证明

部署器和本地测试只证明围栏/顺序，不证明这台服务器已经满足外部门禁。至少留存：

- Alibaba Cloud 加密盘/KMS、挂载与恢复演练证据；
- PostgreSQL 15 真实 TLS、权限、迁移、并发租约与重启恢复；
- MinIO exact API contract、TLS、KES、条件写/checksum、故障恢复；
- 真 Provider 聊天、生图、精修与不确定 POST 行为；
- blue/green 切换、旧 slot 自动恢复、Nginx reload 和备份恢复演练；
- 日志扫描确认没有 bearer、API Key、DSN、Share token 或原始请求正文。

没有加密证明或 MinIO exact contract 时，状态应明确记录为 blocked，不能把 dry-run
或 mock 结果写成已上线。

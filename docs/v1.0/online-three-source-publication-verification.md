# v1 三源在线发布验证

`scripts/verify-v1-online-publication.py` 在不修改任何远程源的前提下，对已签名
Release 生成 Control Plane 和 public Bootstrap index 共用的 canonical
publication receipt。

这条链路明确支持第一源为 GitHub 国内只读代理，例如：

```text
https://ghproxy.net/https://github.com/<owner>/<repo>/releases/download/<tag>
```

代理不需要实现 EcoreX replica 上传 API。验证器只对 manifest 声明的
三个 HTTPS base URL 执行 `GET`，流式计算每个发布文件的精确字节数和
SHA-256。`HEAD` 只能证明元数据可达，不能作为字节摘要证明，因此本工具不使用
`HEAD`。

## 信任与输出

工具先以指定 Ed25519 公钥验证 `release-manifest.json` 和所有 Artifact
签名，再验证 release dir 中的本地字节。manifest 摘要始终来自该文件的
原始字节，不会通过 JSON 重序列化重建。

GitHub 源额外查询 `api.github.com/repos/<owner>/<repo>/releases/tags/<tag>`，
要求：

- tag 与签名 manifest 的 version/channel/release_id 完全一致；
- release ID 为真实正整数；
- `draft=false`；
- GitHub asset 集合与 manifest Artifact 及
  `release-manifest.json` / `release-metadata.json` / `sbom.cdx.json`
  完全一致。

输出文件以 `xb` 模式原子新建，已存在时拒绝覆盖。其 JSON 字节使用
UTF-8、排序 key 和紧凑分隔符，不添加换行，可直接作为
`publication_receipt_sha256` 的权威输入。receipt 严格保持既有七字段
schema，在线调试信息不会混入公开合同。

## 断点续验

checkpoint 只在整个文件已通过 HTTPS GET、size 和 SHA-256 后更新。
它绑定 manifest SHA-256、release ID、GitHub release ID、每个 source/file URL
和字节身份，并使用运行时提供的 32-byte HMAC key 认证。续验时任何
MAC 或身份冲突都会 fail closed。HMAC key 不写入 checkpoint、receipt 或日志。
成功生成 receipt 前会删除 checkpoint；任何下载临时文件也会在成功或失败后
立即删除。

## 运行

PowerShell 示例：

```powershell
$env:ECOREX_PUBLICATION_CHECKPOINT_KEY_BASE64 = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
)
# 私有 GitHub Release 才需要；只能使用 read-only token。
$env:ECOREX_GITHUB_RELEASE_READ_TOKEN = '<read-only token>'

python scripts/verify-v1-online-publication.py `
  --release-dir C:\secure-release\release `
  --output C:\secure-release\publication-receipt.json `
  --checkpoint C:\secure-release\online-verification.checkpoint.json `
  --temporary-directory C:\secure-release\temporary `
  --trusted-public-key 'ecorex-release-2026=<BASE64-32-BYTE-PUBLIC-KEY>' `
  --maximum-total-bytes 17179869184
```

需要恢复时必须重用原 checkpoint HMAC key。不应把它保存在仓库、release
dir 或 shell 历史中。发布成功或放弃 checkpoint 后立即销毁。

默认只允许每个签名 source 的原始 host。GitHub 源额外允许 GitHub 官方
release-asset host。若受控代理有经评审的固定跳转 host，可显式添加：

```text
--allowed-redirect-host github-cn=download-proxy.example.cn
```

该 allowlist 只放行指定 source 的跳转 host。非 HTTPS、URL 用户名/密码、非标准
443 端口、未预期 host、跳转环、过多跳转、size 漂移和摘要漂移全部拒绝。
重试只适用于网络异常和有界的 408/425/429/5xx 状态，不会重试身份或完整性
冲突。

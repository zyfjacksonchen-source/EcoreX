# Linux aarch64 云制品构建与 Windows 脱机签名

v1.0 云端运行时只能在 Linux aarch64 + Python 3.11.9 上构建。构建器要求 `HEAD == origin/main == --expected-commit`，并且除操作员隔离目录 `.artifacts/` 外没有未提交变更。

构建阶段：

```bash
cd /srv/build/EcoreX
/opt/ecorex/platform/python-3.11.9/bin/python3.11 \
  -m venv /var/lib/ecorex/build/v1.0.2/builder-venv-locked
/var/lib/ecorex/build/v1.0.2/builder-venv-locked/bin/python3.11 \
  scripts/install-v1-python-profile.py --profile cloud
/var/lib/ecorex/build/v1.0.2/builder-venv-locked/bin/python3.11 \
  scripts/build-v1-linux-cloud-artifact.py build \
  --source-root /srv/build/EcoreX \
  --artifact-root /var/lib/ecorex/build/v1.0.2/artifact \
  --handoff-root /var/lib/ecorex/build/v1.0.2/signing-handoff \
  --release-id ecorex-cloud-v1.0.2-<candidate> \
  --expected-commit <exact-main-sha>
```

构建器使用 `git archive` 创建不可变源副本，从 `bootstrap.lock` 和 `cloud.lock` 以 `--require-hashes --only-binary=:all: --no-deps` 安装依赖；EcoreX 本身构建为真实 wheel，再按分发名安装。不使用 editable install，不保留指向仓库的 `.pth`。所有依赖、Admin Web package-data、console scripts 和部署模板都在制品树内验证。

Linux 产生四个不含密钥的交接文件：

- `cloud-release-manifest.json`：绑定每个文件的 SHA-256、大小与 `0644/0755` POSIX mode。
- `cloud-release-manifest.signing-payload`：专用 domain prefix + canonical manifest bytes；Windows 只能签名这一串字节。
- `cloud-build-receipt.json`：绑定 commit、locks、应用 wheel、package-data/entrypoint/import 验证和 mode contract。
- `cloud-unsigned-signature-descriptor.json`：绑定上述文件摘要的狭窄签名请求。

Windows DPAPI 只读取 descriptor 与 canonical payload，不接收 Linux 制品树，不导出私钥：

```powershell
python scripts/sign-v1-cloud-manifest-dpapi.py `
  --descriptor C:\handoff\cloud-unsigned-signature-descriptor.json `
  --payload C:\handoff\cloud-release-manifest.signing-payload `
  --output C:\handoff\cloud-manifest-signature-response.json
```

只将 `cloud-manifest-signature-response.json` 返回 Linux。Linux 在 attach 前重新扫描全部字节和 POSIX mode，用发布公钥 keyring 验证 Ed25519 签名，最后才将 manifest/signature 写入制品：

```bash
/var/lib/ecorex/build/v1.0.2/builder-venv-locked/bin/python3.11 \
  scripts/build-v1-linux-cloud-artifact.py attach \
  --artifact-root /var/lib/ecorex/build/v1.0.2/artifact \
  --handoff-root /var/lib/ecorex/build/v1.0.2/signing-handoff \
  --signature-response /var/lib/ecorex/build/v1.0.2/cloud-manifest-signature-response.json \
  --release-keyring /etc/ecorex/release-public-keys.json
```

部署器会再次检查每个文件的 digest、size 和 POSIX mode，任意可执行位丢失、额外文件、符号链接或字节变化都在 staging 前失败。

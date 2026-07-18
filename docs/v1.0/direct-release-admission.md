# Direct release admission（单版本应急上线）

Direct admission 是正式发布协议中的独立、默认关闭的应急入口。它只允许管理员
明确授权的一个 `release_id` 和一份用户指令 SHA-256。它不修改正常 Candidate
门禁，也不会把未执行的 `live-model`、`live-image`、`cdp-acceptance` 伪装成
`passed`。

## 信任边界

- `release-manifest.json`、Artifact 和 Candidate receipt 继续由 release key 验证。
- Candidate receipt、operator waiver、按渠道要求的 publication receipt 和 manifest 的原始
  字节以 Base64 嵌入 direct bundle；服务端验证其精确 SHA-256，而不是重新序列化后
  猜测身份。
- Stable 只要求已签名的 GitHub 国内镜像主源；Canary 继续要求 GitHub 国内镜像、
  GitHub Release 和 EcoreX CDN 都在同一 publication receipt 中包含完全相同的文件集合、
  大小和 SHA-256。
- operator waiver 必须绑定相同 Candidate、commit、instruction SHA 和独立的
  publication key。release/publication key ID 和原始公钥指纹均不得相同。
- final bundle 必须包含 Control Plane 已公开读回并验证的 Bootstrap proof。
- direct bundle 使用 `ecorex-direct-release-admission-v1` 独立签名域，不能被当作普通
  gate bundle 重放。

数据库分别保存正常 `passed/failed` gate 与 direct waiver。三个 live gate 只写入
append-only `direct_release_gate_waivers(status='waived')`；Candidate 投影显示
`waived`。其余所有 required gate 仍必须是 `passed`。prepare 与 finalize 各只允许
一份不可变 attestation，finalize 必须复用 prepare 的 Candidate、waiver、publication
receipt、instruction SHA 和 key roles。

## 生产启用

默认配置为：

```text
ECOREX_CP_DIRECT_RELEASE_ADMISSION_ENABLED=false
```

需要使用时，只能在部署链已经验证为 root 持有、`0640`、位于加密卷的
`control-plane.secret.env` 中一次性同时设置：

```text
ECOREX_CP_DIRECT_RELEASE_ADMISSION_ENABLED=true
ECOREX_CP_DIRECT_RELEASE_ID=release-stable-<24-lowercase-hex>
ECOREX_CP_DIRECT_RELEASE_INSTRUCTION_SHA256=<64-lowercase-hex>
```

少一个值、关闭时残留 release/instruction authority、目标不是当前 bundle，服务均
拒绝启动或拒绝 admission。上线完成后删除后两项并恢复 `false`，重启 Control Plane。
不要把 direct authority 复制到普通 `control-plane.env`。

Nginx 只对锚定的
`PUT /api/v1/admin/releases/<safe-id>/direct-admission` 使用
`client_max_body_size 32m`，普通 release/admin 路由不放宽。读取或缓冲大请求体前，
Nginx 先通过 internal、no-body `auth_request` 验证 Bearer 和 `release_admin`；未认证
请求不会进入大 body 缓冲。通过后仍有 10 秒 body timeout、64 KiB body buffer 和
显式 request buffering。Control Plane 再次在读取 body 前执行同一认证，并以单个
in-flight 内存槽、准确 `Content-Length` 和 32 MiB 上限提供纵深保护。

## 两阶段命令链

下列命令中的 key、endpoint、receipt 和 journal 均需使用同一 release 的真实值。
`$STAGING_RUN_ID` 是生成 Candidate receipt 中三平台 Stage 的
`ecorex-v1-platform-stage.yml` 运行 ID；`$CANDIDATE_RUN_ID` 是生成非发布
gate receipt 的 `ecorex-v1-candidate.yml` 运行 ID。两者必须是同一 commit 上两个
不同的真实运行，不能互换，也不能把一个 ID 手工写入另一类 receipt。

1. 使用 `build-v1-direct-operator-release.py` 生成签名 Candidate 和
   `direct-release-waiver.json`。waiver 明确记录 protected live acceptance 为
   `not-run`/`operator-waived`，不能写 `passed`。
2. 三源发布完成后组装和签名 prepare bundle：

```powershell
python scripts/assemble-v1-direct-release-admission.py `
  --phase prepare --manifest release-manifest.json `
  --candidate-receipt candidate-build-receipt.json `
  --operator-waiver direct-release-waiver.json `
  --publication-receipt publication-receipt.json `
  --receipts-dir gate-receipts `
  --expected-commit $COMMIT `
  --expected-staging-run-id $STAGING_RUN_ID `
  --expected-candidate-workflow-run-id $CANDIDATE_RUN_ID `
  --operator-instruction-sha256 $INSTRUCTION_SHA `
  --output direct-prepare.unsigned.json

python scripts/sign-v1-direct-release-admission.py `
  --unsigned direct-prepare.unsigned.json `
  --manifest release-manifest.json `
  --publication-key-description publication-key.json `
  --operator-instruction-sha256 $INSTRUCTION_SHA `
  --output direct-prepare.signed.json
```

3. 调用既有 promotion flow 创建 Candidate 和 draft rollout；不会发布或激活：

```powershell
python -m ecorex.control_plane.cli promote --direct-admission --phase prepare `
  --manifest release-manifest.json --evidence direct-prepare.signed.json `
  --publication-receipt publication-receipt.json `
  --trusted-key "$RELEASE_KEY" --trusted-publication-key "$PUBLICATION_KEY" `
  --operator-instruction-sha256 $INSTRUCTION_SHA `
  --journal promotion.json --percentage 100
```

4. 使用既有 `stage-public-bootstrap-index` 和 `activate-public-bootstrap-index`
   命令完成 CAS 激活与公网 readback，取得 Bootstrap receipt。
5. 用相同的 `$STAGING_RUN_ID`、`$CANDIDATE_RUN_ID` 和其他输入，加上
   `--phase finalize --bootstrap-index-receipt ...` 重新 assemble/sign。服务端会
   逐项比较 prepare，任何 drift 都拒绝。
6. final promotion 先记录 final admission、验证可信 Bootstrap proof、发布 Candidate，
   最后才激活既有 rollout：

```powershell
python -m ecorex.control_plane.cli promote --direct-admission --phase finalize `
  --manifest release-manifest.json --evidence direct-final.signed.json `
  --publication-receipt publication-receipt.json `
  --bootstrap-index-receipt bootstrap-index-readback-receipt.json `
  --trusted-key "$RELEASE_KEY" --trusted-publication-key "$PUBLICATION_KEY" `
  --operator-instruction-sha256 $INSTRUCTION_SHA `
  --journal promotion.json --percentage 100 --activate
```

相同 `client_request_id` 的网络重试由 Control Plane 幂等表回放；不同内容不能复用
prepare/finalize phase。任何 embedded evidence、签名、key role、publication origin 或
Bootstrap proof 不匹配时，release 保持 Candidate，rollout 不会激活。

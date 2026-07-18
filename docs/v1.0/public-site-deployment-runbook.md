# Public Web/Admin 原子上线手册

这个步骤只切换 `https://dl.ecoremedia.net/ecorex-agent/` 的公开 Web 站点。
`/ecorex-agent/admin/` 仍由 Nginx 反向代理到 loopback Control Plane，不会把管理
端静态文件或凭据复制到公开站点。部署器不支持 SSH、上传、构建、
签名或自定义域名，因此不能绕过上游发布校验。

## 固定服务器边界

- 公开根目录：`/srv/ecorex-agent-download`
- 待发布输入：`site-staging/<release_id>/site`
- direct checker 收据：`site-staging/<release_id>/direct-deployable.json`
- 独立部署授权：`site-staging/<release_id>/deployment-authorization.json`
- 不可变 slot：`site-slots/<release_id>`
- 当前指针：`current -> site-slots/<release_id>`
- 动态 Bootstrap 指针：`public-pointer/public-bootstrap-index.json`
- 旧实体目录备份：`legacy-sites/pre-v1-<transaction_id>`
- 持久 journal/收据：`/var/lib/ecorex/site-deploy`
- 共享产品部署锁：`/run/lock/ecorex-cloud-deploy.lock`
- 固定二进制：`/usr/sbin/nginx`、`/usr/bin/systemctl`、`/usr/bin/curl`
- 固定发布验签 keyring：`/etc/ecorex/cloud/release-public-keys.json`
- 固定 freshness 验签 keyring：`/etc/ecorex/cloud/publication-public-keys.json`

staging 目录必须由 root 拥有，不得被 group/other 写入。`site` 中只能有
`index.html`、`public-bootstrap-index.json`、各一个内容寻址的 JS/CSS
和 `assets/` 内 HTML 精确引用的内容寻址资产。链接、中间文件、日志、
源码映射和额外空目录都会阻断上线。

### 一次性 loopback TLS readback 基线

如果公网 TLS 由 CDN 终止，而源站证书属于另一个站点，生产初始化必须一次性
建立 `dl.ecoremedia.net` 专用的 loopback SNI vhost。它只监听
`127.0.0.1:443`、复用正式静态目录与 Cloud/Admin route include，并使用只包含
`dl.ecoremedia.net` SAN 的独立 readback leaf。独立 CA 私钥和 leaf 私钥必须
root-only；CA 只安装到该生产主机的系统 trust store。它可以与 Provider Bridge
共享同一个 loopback listener，因为 Nginx 按 SNI 选择完全分离的证书和路由。

这是主机 bootstrap 控制，不是每个版本的发布步骤。建成后，每次发布仍由下方
部署器执行真实 SNI、完整证书校验和逐字节 readback；不得使用 `curl -k`、关闭
hostname 校验或退化为 HTTP。若该 vhost/CA 缺失或证书选择漂移，apply 会在提交
前失败并恢复上一站点。

## 生成已校验 staging

先在管理员发布工作站完成三源发布、签名指针生成和 direct deployable
checker。checker stdout 只是已校验数据，不是部署权威；将其以单行
canonical JSON 保存为 `direct-deployable.json` 后，在 Windows 发布工作站调用：

```powershell
python scripts/sign-v1-public-site-deployment.py `
  --release-id release-stable-REPLACE_WITH_24_HEX `
  --staging-release-dir C:\ecorex-admin\site-staging\release-stable-REPLACE_WITH_24_HEX `
  --cloud-artifact-root C:\ecorex-admin\cloud\ecorex-cloud-v1.0.0-REPLACE
```

命令复用 digest-pinned `DigestPinnedExternalSigner` 和现有
`ECOREX_RELEASE_SIGNER_*` 环境配置；DPAPI adapter 会选择 release key，不会选择
freshness/publication key。签名字节为
`ecorex.public-site-deployment.v1\0 || canonical(authorization)`，严格绑定 release/version、
manifest、waiver、三源收据、公开指针、direct receipt 字节摘要和完整站点
tree digest。服务器会用固定 keyring 验签，然后再重扫一次 tree；任何收据、
站点、key ID、签名或 domain 篡改都会在创建 slot 之前阻断。
授权同时验证并绑定同一个 release-key-signed Linux cloud manifest，其中的 Admin
rendered index、CSS/JS 内容寻址摘要、产品版本标记和 ready 响应都进入签名负载。

然后仅把上述精确站点字节、checker 收据和部署授权上传到固定 staging
路径。不要直接把代码仓库的 `deploy/ecorex-site`
复制到生产：仓库指针是故意保持的 `unpublished` 占位文档。

`public-bootstrap-index.json` 是签名 staging 输入，但不会复制进不可变
`site-slots`。Control Plane 在独立 CP-owned 目录中以 compare-and-swap 原子续签
freshness；Nginx/Caddy 只有一个 exact route 指向该动态对象。站点授权仍绑定初始
指针摘要。apply/readback 使用 release keyring 验 immutable authority、使用
publication keyring 验 freshness，只允许 freshness 窗口变化；未知 key、过期/伪造
签名及 release/target/source 任一漂移都会阻断。

首次接管旧 Nginx 路由时，cloud deployer 在撤除旧 exact location 前先稳定读取并
验证旧 pointer，再原子 seed 到 `public-pointer`；紧邻改写 server config 前会再次
读取 source/target 并要求 exact bytes、长度和 SHA-256 与 seed 相同。任何中途更新、
签名漂移或双 authority 冲突都保留旧路由并 fail-closed。若旧站原本没有 exact
pointer 路由且公网返回 404，则只创建规范的 canonical `unpublished` 文档，不伪造
可下载版本；随后新 exact route 可以安全返回 200，正式 release 仍须经过签名激活。

## 只读计划

```bash
sudo /opt/ecorex/platform/python-3.11.9/bin/python3.11 \
  /opt/ecorex/operator/scripts/deploy-v1-public-site.py \
  --release-id release-stable-REPLACE_WITH_24_HEX \
  --dry-run
```

dry-run 不获取锁、不创建 slot、不修改 `current`、不 reload Nginx、不访问
公网。输出必须是 `mutation_performed=false`，且 `public_index_sha256`、
`site_tree_sha256`与发布记录一致。`pending_recovery=true` 表示上一次进程在
提交点前中断；不要删除 journal，下一次 apply 会根据真实 `current` 状态
完成或回退。

## 原子激活

```bash
sudo /opt/ecorex/platform/python-3.11.9/bin/python3.11 \
  /opt/ecorex/operator/scripts/deploy-v1-public-site.py \
  --release-id release-stable-REPLACE_WITH_24_HEX \
  --apply \
  --confirm-target https://dl.ecoremedia.net/ecorex-agent/
```

apply 只在 Linux root、固定路径和完整目标确认串下生效。它依次：

1. 获取与 cloud sidecar 共享的排他 `flock`；
   首次运行会把旧 uid 994 拥有的 download root 接管为 `root:994 0755`；
   `site-slots` 为 `root:994 0755`，staging/legacy 为 `root:root 0700`。所有路径
   必须同设备、无预置 symlink/hardlink，否则在 chown/copy 前失败。
2. 恢复未完成 journal，根据真实指针而不是单独信任 phase 字段；
3. 在 `site-slots` 同文件系统写入临时目录，逐文件 fsync，重新计算
   精确文件集和 tree digest，再以 no-replace 原子 rename 固化 slot；动态
   `public-bootstrap-index.json` 不进入该 slot；
4. 写入 root-only journal；
5. 原子切换 `current`。旧版 `current` 若是实体目录，使用 Linux
   `renameat2(RENAME_EXCHANGE)` 交换，不留空窗；
6. 执行 `nginx -t`、reload 和 active 检查；
7. 通过 `127.0.0.1:443` 但使用真实 SNI/Host 做 HTTPS readback：HTML/指针
   必须 `no-store`、每个 hash 资产必须一年 immutable、`/admin/` 必须
   精确匹配授权中的 rendered index，逐个匹配内容寻址 CSS/JS，且包含
   `no-store`、固定 CSP、`X-EcoreX-Product-Version: 1.0.0`；
   `/ecorex-agent/admin/health/ready` 还必须返回签名授权绑定的 ready JSON。
8. 写入 0600 typed receipt，最后删除 journal 作为唯一提交点。

任一步失败都会先原子恢复之前的 symlink 或旧版实体目录，reload Nginx
并对旧 `index.html` 做 HTTPS 精确字节复验。如果回退本身无法复验，命令返回
`site_activation_recovery_required` 并保留 journal；禁止手工删除 slot、backup 或
journal。

## 上线收据

成功输出的 `receipt` 固定位于
`/var/lib/ecorex/site-deploy/receipts/<release_id>.json`，权限为 0600。记录包含
direct checker 收据摘要、站点 tree/指针摘要、上一版类型、Nginx 结果与
每个 HTTPS readback 的摘要。相同 release/tree 重试只会复验在线内容并返回
同一收据，不会覆盖 slot 或重写收据。

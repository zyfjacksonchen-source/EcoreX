# mvdcm.ecoremedia.net 无扰克隆计划

## 目标

把当前 EcoreX 管理后台、Admin API 数据、下载页面克隆到新服务器和新域名：

- 域名：`mvdcm.ecoremedia.net`
- 目标面板：已由用户提供，凭据不写入仓库。

前提：不影响当前稳定使用中的后台。新环境独立部署、独立数据目录、独立端口，验证通过后再做无痛切换。

## 当前生产约定

仓库中的现有部署约定：

- 下载页静态发布目录：`/srv/ecorex-agent-download/current`
- 下载页 release 根目录：`/srv/ecorex-agent-download/releases`
- Admin API 根目录：`/srv/ecorex-agent-admin`
- Admin API 应用目录：`/srv/ecorex-agent-admin/app`
- Admin API 数据目录：`/srv/ecorex-agent-admin/data`
- Admin API 环境文件：`/srv/ecorex-agent-admin/env/ecorex-admin-api.env`
- Admin API 默认监听：`127.0.0.1:18084`
- WebUI runtime 示例监听：`127.0.0.1:9909`

## 迁移原则

1. 只读备份旧环境，不在旧机器上执行更新、重启或配置覆盖。
2. 新机器使用独立目录，避免与未来其他站点冲突。
3. SQLite 使用在线安全备份或停写窗口复制，避免直接复制 WAL 未合并状态。
4. 静态下载页和 release 包可以直接复制，不依赖旧服务运行态。
5. 新域名先独立验证，确认无误后再切 DNS 或入口。

## 推荐新环境目录

```text
/srv/ecorex-agent-download
/srv/ecorex-agent-download/releases
/srv/ecorex-agent-download/current
/srv/ecorex-agent-admin
/srv/ecorex-agent-admin/app
/srv/ecorex-agent-admin/data
/srv/ecorex-agent-admin/env
/srv/ecorex-agent-admin/server
/srv/ecorex-agent-admin/backups
```

## 需要复制的内容

### 下载页面

复制旧环境：

```text
/srv/ecorex-agent-download/current
/srv/ecorex-agent-download/releases
```

也可以用当前 release zip 重新安装，再校验 `manifest.json` 和 `downloads/`。

### Admin API 程序

复制或重新部署：

```text
/srv/ecorex-agent-admin/app/ecorex_admin_api.py
/srv/ecorex-agent-admin/server
```

### Admin API 数据

核心文件：

```text
/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3
```

若存在 SQLite WAL/SHM：

```text
/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3-wal
/srv/ecorex-agent-admin/data/ecorex-admin.sqlite3-shm
```

优先用 SQLite `.backup` 生成一致性备份，而不是裸复制运行中数据库。

### 环境配置

复制并审阅：

```text
/srv/ecorex-agent-admin/env/ecorex-admin-api.env
```

必须保留：

- `ECOREX_ADMIN_DB`
- `ECOREX_CLIENT_EVENT_KEYS`
- Admin 登录凭据
- 模型/客户端相关配置
- `ECOREX_ALLOWED_ORIGINS`

新域名上线前应加入：

```text
ECOREX_ALLOWED_ORIGINS=https://mvdcm.ecoremedia.net
```

如需要兼容旧域名测试，可逗号分隔同时保留旧域名。

## 新机器部署步骤

1. 登录新面板，只创建新站点和独立运行服务，不改旧生产。
2. 确认系统组件：Python 3、Nginx/Caddy、systemd 或面板进程管理。
3. 创建目录和运行用户，例如 `ecorex`。
4. 上传或拉取 release 包。
5. 安装静态站点到 `/srv/ecorex-agent-download/current`。
6. 部署 Admin API 到 `/srv/ecorex-agent-admin/app`。
7. 放入克隆数据库到 `/srv/ecorex-agent-admin/data`。
8. 放入环境文件到 `/srv/ecorex-agent-admin/env/ecorex-admin-api.env`。
9. 启动 Admin API，监听 `127.0.0.1:18084`。
10. 配置站点反代：
    - `/ecorex-agent/admin/api/*` -> `127.0.0.1:18084`
    - `/ecorex-agent/api/admin/*` -> `127.0.0.1:18084`
    - `/ecorex-agent/client/*` -> `127.0.0.1:18084`
    - `/ecorex-agent/admin/*` -> 静态 admin 目录
    - `/ecorex-agent/*` -> 静态下载页目录
11. 配置 HTTPS 证书。
12. 用新域名完整验证。

## 验证清单

公开下载页：

```text
https://mvdcm.ecoremedia.net/ecorex-agent/
https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json
```

后台：

```text
https://mvdcm.ecoremedia.net/ecorex-agent/admin/
https://mvdcm.ecoremedia.net/ecorex-agent/admin/api/state
```

客户端：

```text
https://mvdcm.ecoremedia.net/ecorex-agent/client/capability-policy
https://mvdcm.ecoremedia.net/ecorex-agent/client/model-config
```

预期：

- 下载页 HTTP 200。
- manifest HTTP 200，版本和 SHA256 正确。
- Admin 页面未认证时要求登录。
- Admin API 使用凭据后能读取用户、模型、用量和日志。
- 客户端接口无用户 token 时不能泄露模型密钥。

## 切换策略

1. 新域名先独立运行。
2. 管理员验收新后台数据完整。
3. 客户端策略切到新域名进行小流量测试。
4. 观察无误后，再将正式入口切到新站。
5. 旧站保留只读观察窗口，直到确认无需回滚。

## 回滚策略

切换前不修改旧环境，因此回滚只需要：

1. DNS 或入口切回旧域名。
2. 客户端 enterprise policy 切回旧 Admin API。
3. 保留新站日志用于排查。

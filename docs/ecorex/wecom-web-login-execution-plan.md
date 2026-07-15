# EcoreX 企业微信 Web 登录执行计划

## 目标

网页端接入企业微信扫码登录。用户首次扫码成功后，自动在 EcoreX Admin 后台创建账号；后续扫码直接复用已绑定账号登录。

第一版按企业自建应用 `CorpApp` 实现，暂不做服务商多企业模式。

## 企业微信后台准备

1. 创建企业微信自建应用。
2. 记录 `CorpID`、`AgentID`、应用 `Secret`。
3. 开启企业微信 Web 登录能力。
4. 配置可信回调域名，例如：
   `https://www.ecoreai.cn/ecorex-agent/client/auth/wecom/callback`
5. 确认应用可见范围，只包含允许使用 EcoreX 的员工。
6. 如需读取姓名、邮箱等详细资料，补齐应用权限和可信 IP。

## 后端配置

新增环境变量：

```text
ECOREX_WECOM_SSO_ENABLED=1
ECOREX_WECOM_LOGIN_TYPE=CorpApp
ECOREX_WECOM_CORP_ID=...
ECOREX_WECOM_AGENT_ID=...
ECOREX_WECOM_SECRET=...
ECOREX_WECOM_REDIRECT_URI=...
ECOREX_WECOM_DEFAULT_ROLE=member
ECOREX_WECOM_DEFAULT_DAILY_TOKEN_LIMIT=0
ECOREX_WECOM_DEFAULT_WEEKLY_TOKEN_LIMIT=0
```

## 后端接口

在 Admin API 增加：

```text
GET  /client/auth/wecom/login-url
GET  /client/auth/wecom/callback
POST /client/auth/wecom/exchange
```

- `login-url` 生成企业微信登录链接：
  `https://login.work.weixin.qq.com/wwlogin/sso/login?login_type=CorpApp&appid=CORP_ID&agentid=AGENT_ID&redirect_uri=REDIRECT_URI&state=STATE`
- `callback` 接收企业微信回调的 `code` 和 `state`，校验后换取企业微信身份。
- `exchange` 用一次性登录票据换取 EcoreX 当前 session 格式：`token/user/quota/expiresAt/deviceId`。

不要把正式 EcoreX token 放在 URL 中；callback 成功后只生成一次性 `login_ticket`。

## 数据库迁移

新增外部身份绑定表：

```sql
CREATE TABLE external_identities (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  corp_id TEXT NOT NULL,
  external_user_id TEXT NOT NULL,
  open_userid TEXT,
  user_id TEXT NOT NULL,
  raw_profile TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, corp_id, external_user_id)
);
```

新增一次性登录票据表：

```sql
CREATE TABLE sso_login_tickets (
  id TEXT PRIMARY KEY,
  ticket_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  device_id TEXT,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  created_at TEXT NOT NULL
);
```

## 自动建号规则

1. 用 `corp_id + userid` 查询 `external_identities`。
2. 已绑定则直接登录。
3. 未绑定则读取成员详情，创建 `users` 记录。
4. 默认角色为 `member`，不自动创建管理员。
5. 若企业微信返回邮箱，则使用企业邮箱。
6. 若没有邮箱，生成稳定占位邮箱：`wecom-<hash>@ecorex.local`。
7. 写入审计事件：`wecom.user.auto_create` 或 `wecom.user.login`。

## 前端改造

涉及：

- `desktop/src/App.tsx`
- `desktop/src/services/ecorexApi.ts`
- `channel/web/web_channel.py`

登录页新增“企业微信扫码登录”入口。扫码成功后，用一次性 `login_ticket` 换取正式 session，再写入现有 WebUI 企业 session 存储。

SSO 用户的账号设置页显示“企业微信账号”，隐藏或弱化“修改密码”。

## 管理后台展示

涉及：

- `deploy/ecorex-site/admin/index.html`
- `deploy/ecorex-site/admin/admin.js`

建议展示：

- 登录来源：邮箱密码 / 企业微信。
- 企业微信 UserID。
- 最近 SSO 登录时间。

后台仍保留禁用、删除、改角色、改额度和重置密码能力。

## 验收清单

- 登录 URL 参数正确，`state` 有效。
- `state` 过期、重复、篡改会失败。
- 首次扫码自动建号。
- 重复扫码不会重复建号。
- 禁用用户扫码失败。
- 默认额度生效。
- 后台用户列表能看到自动创建账号。
- 用量和错误日志仍归集到 EcoreX 用户。
- `login_ticket` 只能使用一次。

## 官方文档

- 企业微信 Web 登录开始开发：`https://developer.work.weixin.qq.com/document/path/98170`
- Web 登录组件：`https://developer.work.weixin.qq.com/document/path/98171`
- 获取登录用户身份：`https://developer.work.weixin.qq.com/document/path/98179`

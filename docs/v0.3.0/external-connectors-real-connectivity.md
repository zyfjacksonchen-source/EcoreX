# EcoreX v0.3.0 External Connectors Real Connectivity Matrix

Date: 2026-07-07
Scope: WebUI external connector productization, runtime discovery, and online-update preservation.

## Product Rule

EcoreX must not show a connector as connectable unless the backend can actually store credentials/config, discover the tool/channel after a new session or update, and run a health check. Workbuddy-like UI is acceptable only as a shell over real runtime capability, not as a planned-catalog facade.

## Implemented In This Slice

| Connector | Runtime path | Connection method | Discovery/health |
| --- | --- | --- | --- |
| Tencent Docs | Workspace `mcp.json` + `tencent-docs` MCP | Official Tencent Docs MCP endpoint `https://docs.qq.com/openapi/mcp`; token written to workspace MCP config | `/api/tencent-docs/status?start=1`; `ToolManager.ensure_mcp_configured_loaded()`; attachment flow waits for MCP readiness |
| Feishu/Lark | Existing CowAgent/EcoreX `feishu` channel + `feishu_cli` tool | App credentials and/or agent auth action where available | `/api/external-connections`; skill/tool binding sees `feishu_cli`; update health snapshot preserves callable/connected state |
| DingTalk | Existing `dingtalk` channel | Credential-backed channel config | `/api/external-connections`; update health snapshot |
| WeCom App/Bot | Existing `wechatcom_app` and `wecom_bot` channels | Credential/webhook-backed channel config | `/api/external-connections`; update health snapshot |
| QQ | Existing `qq` channel | Credential-backed channel config | `/api/external-connections`; update health snapshot |

## Researched But Not Exposed As Buttons

| Product | Official connection route found | EcoreX v0.3.0 decision |
| --- | --- | --- |
| Tencent Meeting | Tencent Meeting Open API/OAuth2 exists, API host examples use `https://api.meeting.qq.com` and some APIs support OAuth2 | Not exposed. Current repo has no real meeting connector/tool, and third-party access/permissions need a proper backend integration and health probe. |
| Tencent Survey | Tencent Survey OpenAPI supports team users/questionnaire data and embedding flows | Not exposed. Current repo has no survey connector/tool or credential lifecycle. |
| QQ Mail | No production-ready official EcoreX connector found in repo; generic mail could be implemented separately through IMAP/SMTP/auth code | Not exposed. Avoid presenting QQ Mail as an official Workbuddy-style connector until backend tool calls and account authorization exist. |
| Lexiang Knowledge Base | Lexiang OpenAPI uses AppKey/AppSecret, permissions, bearer access token, and AI search/Q&A APIs | Not exposed. API route is real, but EcoreX has no bundled Lexiang tool/skill runtime in this repo yet. |
| ima Knowledge Base | ima API Key / client id route is documented through `https://ima.qq.com/agent-interface` and related skill ecosystem docs | Not exposed. Need verified official API contract, credential storage, tool implementation, and health checks before UI exposure. |
| Tongdaxin/finance connector | No matching official EcoreX connector found in the repo or original CowAgent config scan | Not exposed. Do not show as a connector until a real backend adapter exists. |

## Original CowAgent / Local Repo Findings

- Original CowAgent config template exposes Feishu, DingTalk, and WeCom credential keys, matching existing EcoreX channel families.
- Current EcoreX repo has channel implementations for Feishu, DingTalk, WeCom app/bot, QQ, Slack, Telegram, Discord, WeChat variants, and Web.
- Tencent Docs is implemented as a workspace MCP connector in EcoreX, not as a legacy CowAgent channel.
- No real backend connector was found for Tencent Meeting, Tencent Survey, QQ Mail, Lexiang, ima, or Tongdaxin in the current repo scan.

## Update Preservation Contract

- Release packages must keep user config outside the immutable runtime package.
- Workspace `mcp.json`, state `appdata`, and state `tools` are treated as preserved connector roots.
- The installer captures `before` and `after` connector snapshots:
  - `/api/external-connections`
  - `/api/tencent-docs/status?start=1`
- If any previously connected/callable connector is missing after the new runtime starts, update state becomes `rollback` and the version is not considered safe to switch to.

## Source Links

- Tencent Docs Open Platform: https://docs.qq.com/open/document/
- Tencent Docs OAuth: https://docs.qq.com/open/document/app/oauth2/
- Tencent Meeting Open API: https://meeting.tencent.com/open-api.html
- Tencent Meeting OAuth/API usage: https://meeting.tencent.com/support-doc-detail/776/index.html
- Tencent Meeting API example: https://meeting.tencent.com/support-doc-detail/556/index.html
- Tencent Survey OpenAPI: https://wj.qq.com/docs/openapi
- Tencent Survey URL/API example: https://wj.qq.com/docs/v23.11/openapi/survey/get_url/
- Feishu access token guide: https://open.feishu.cn/document/server-docs/api-call-guide/calling-process/get-access-token?lang=zh-CN
- Feishu tenant access token: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
- DingTalk user token: https://open.dingtalk.com/document/development/obtain-user-token
- DingTalk internal app access token: https://open.dingtalk.com/document/development/obtain-the-access-token-of-an-internal-app
- Lexiang OpenAPI start: https://lexiang.tencent.com/wiki/api/
- Lexiang AI search: https://lexiang.tencent.com/wiki/api/40004.html
- ima API Key entry: https://ima.qq.com/agent-interface

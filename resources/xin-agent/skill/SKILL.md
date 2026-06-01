---
name: xin-agent
description: Use the EcoreX-managed Xin Assistant CLI for read-only advertising data queries, including XHS Spotlight, XHS Chengfeng, and Bilibili account, project, and report data. Trigger when the user asks for 芯助手, 小红书, 聚光, 乘风, B站, account list, project list, report summary, or advertising performance data.
---

# Xin Assistant CLI

Use `xin-agent-query` for read-only enterprise advertising data. Do not use MCP, browser automation, or arbitrary shell for this data source.

The command always prints one JSON object:
- success: `ok=true`, payload in `data`
- failure: `ok=false`, details in `error`

Allowed commands:

```bash
xin-agent-query --command "schema"
xin-agent-query --command "account list" --platform xhs --xhs-channel spotlight --limit 500 --offset 0
xin-agent-query --command "account list" --platform xhs --xhs-channel chengfeng --limit 500 --offset 0
xin-agent-query --command "account list" --platform bili --limit 500 --offset 0
xin-agent-query --command "project list" --platform xhs --xhs-channel spotlight --account-id 10813275 --start-date 2026-05-26 --end-date 2026-05-31 --limit 500 --offset 0
xin-agent-query --command "report summary" --platform xhs --xhs-channel spotlight --account-id 237568 --start-date 2026-05-26 --end-date 2026-05-31 --limit 500 --offset 0
xin-agent-query --command "report summary" --platform xhs --xhs-channel chengfeng --account-id 10835330 --start-date 2026-05-26 --end-date 2026-05-31 --limit 500 --offset 0
xin-agent-query --command "report summary" --platform bili --account-id 4259258 --start-date 2026-05-01 --end-date 2026-05-31 --limit 500 --offset 0
xin-agent-query --command "project detail" --project-id 1 --limit 100
xin-agent-query --command "task list" --project-id 1 --include-archived --limit 100
xin-agent-query --command "user list" --include-resigned --limit 100
xin-agent-query --command "sync state"
xin-agent-query --command "sync changes" --since "2026-06-01 00:00:00" --limit 100
```

Rules:
- `limit` must be 1 to 500. Use `--offset` for pagination.
- `sync changes` returns added/updated rows since the provided timestamp.
- Use `sync state` row counts/fingerprints to detect physical deletes or soft deletes, then refresh the matching list/detail command.
- For XHS Spotlight use `--xhs-channel spotlight`.
- For XHS Chengfeng use `--xhs-channel chengfeng`.
- Distinguish returned account/app lines by `app_id`: `9700` local Spotlight, `9704` medical Spotlight, `9703` local Chengfeng, `2061267488263258112` Bilibili.
- If the CLI returns `forbidden`, say the data source is admin-only.
- Final output should be concise: key count, key rows, and any missing parameter or error. Do not paste raw JSON unless the user asks.

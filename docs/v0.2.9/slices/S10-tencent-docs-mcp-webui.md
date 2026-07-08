# S10 Tencent Docs MCP WebUI Out-of-Box Capability

## Status

Completed.

## Source

- Origin thread: `019f2d72-6829-76a1-9a15-1d4daad0386c`.
- User boundary: WebUI only.
- Experience reference: WorkBuddy-style Tencent Docs flow where selected documents can be added to the current task.

## Intent

Add Tencent Docs MCP as a visible WebUI capability so users can connect a Tencent Docs token, browse or search documents, multi-select documents, and add them to the current conversation/task context as remote attachments.

## Decisions

- Scope is WebUI plus the minimal backend API needed by the WebUI.
- Do not add CLI flows, generic MCP marketplace work, release-site changes, scheduled-task binding, or project long-term document binding in this slice.
- Use the official remote MCP endpoint `https://docs.qq.com/openapi/mcp`.
- The token is provided by the user from Tencent Docs MCP authorization, stored only in local workspace `mcp.json`, and passed as the `Authorization` header.
- API responses, logs, and status summaries must never echo the token.
- Use the existing MCP configure/reload path and register the server as `tencent-docs`.
- Add Web APIs for status, connect, disconnect, and file listing/search.
- WebUI capability status should combine local MCP config, runtime MCP status, and discovered Tencent Docs tool count.
- The composer gets a Tencent Docs entry point: disconnected state opens a token connection dialog, connected state opens a document selector.
- The selector supports recent documents, my documents, search, multi-select, and a fixed action to add selected documents to the current task/conversation.
- If the runtime MCP tools do not expose a recent-document tool, the recent tab may fall back to my documents.
- Remote document attachments keep a stable key such as `tencent-docs://<file_id>` and optional metadata including `provider`, `source`, `file_id`, `node_id`, `doc_type`, `url`, `owner`, and `updated_at`.
- Backend message handling must not read Tencent Docs attachments as local files.
- Hidden context should include the selected document title, id, node id, URL, and a clear instruction to use discovered Tencent Docs MCP content tools when content is needed.
- Unless the user explicitly requests document mutation, the agent should not proactively create, modify, or delete Tencent Docs documents.
- Frontend code should not hard-code final `mcp__...` tool names; backend/runtime discovery owns MCP tool-name mapping.

## Candidate Implementation

Implemented:

- Added WebUI backend APIs:
  - `GET /api/tencent-docs/status`
  - `POST /api/tencent-docs/connect`
  - `POST /api/tencent-docs/disconnect`
  - `GET/POST /api/tencent-docs/files`
  - `GET/POST /api/tencent-docs/search`
- Wrote local workspace `mcp.json` config for server `tencent-docs` using `type=streamable-http`, official endpoint `https://docs.qq.com/openapi/mcp`, and `Authorization` header.
- Kept token out of status/list responses and public summaries.
- Added runtime MCP status/tool-count projection and heuristic file-list normalization from discovered Tencent Docs MCP tools.
- Added permission-broker default allow only for `mcp_server` startup against the exact official Tencent Docs MCP endpoint.
- Extended WebUI attachment persistence/runtime parsing with remote provider metadata.
- Added Tencent Docs remote hidden context in both Web message paths, explicitly instructing the agent to use discovered `tencent-docs` MCP tools and treat documents as read-only unless the user asks for mutation.
- Added composer Tencent Docs entry point, token connection dialog, recent/my/search document picker, multi-select, attachment-tray addition, and remote open-link handling.
- Preserved retry drafts/history for Tencent Docs remote attachments.
- Added focused tests in `tests/test_v029_tencent_docs_mcp.py`.

## Verification Plan

Completed:

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_tencent_docs_mcp.py -q` passed: 4 tests.
- `npm run typecheck` in `desktop/` passed.
- `npm run build:renderer` in `desktop/` passed; Vite emitted the existing renderer chunk-size warning.
- Combined focused Python check passed: `tests/test_v029_tencent_docs_mcp.py`, `tests/test_v029_release_metadata.py`, the WebUI installer consistency test, Admin release-notice test, and WebChannel enterprise release-notice key test.
- Do not run `scripts/真实发布校验.py`.

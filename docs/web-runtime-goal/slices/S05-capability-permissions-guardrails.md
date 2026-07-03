# S5: Capability Permissions And Usable Guardrails

## Goal

Make the Web permission model usable by default without weakening high-risk operations. The slice introduces a public capability-level authorization boundary so Web APIs, AgentStream, scheduler background work, and image jobs share one broker and one audit stream.

## Changes

- Added `ToolPermissionBroker.authorize_capability()` and top-level `authorize_capability()` in `common/ecorex_tool_permissions.py`.
- Added default low-risk allow rules for:
  - `optional_abilities` status/list.
  - `agent_capability` diagnose/list/list_packs.
  - `scheduler` list/get/status/projection.
  - `image_jobs` status/collect/list and safe cancel.
  - `browser` snapshot/status/list/get.
  - `workspace` and `artifact` read through `authorize_file_access()`.
- Added explicit bash capability boundaries:
  - `workspace_read` and `workspace_write` go through the filesystem profile.
  - `system_shell` requires interactive confirmation, full-access mode, or an explicit remembered shell grant; read-only mode always denies it.
- Tightened `feishu_cli run`:
  - read-like CLI commands are allowed by default.
  - write/admin commands are denied in read-only mode and require prompt/full-access in other modes.
  - structured install/config/auth actions are classified as configure actions; read-only blocks them, smart-ask requires confirmation, and noninteractive smart-ask denies them.
- Switched AgentStream tool authorization to `authorize_capability()`.
- Switched scheduler background execution and scheduled tool calls to `authorize_capability()`.
- Added Web API capability checks for:
  - image job status/collect/start/cancel.
  - scheduler list and mutations.
  - Feishu external-connection agent authorization before direct CLI execution.
- Tightened Web file/artifact access helpers so malformed broker decisions, missing `allowed`, or non-dict return values fail closed instead of becoming truthy allows.
- Tightened AgentStream and scheduler compatibility so a malformed `authorize_capability()` result fails closed instead of falling through to a permissive legacy broker method.
- Moved image job parallelism policy resolution into the shared `agent.protocol.image_job_service` runtime layer and exported it from `agent.protocol`; Web now calls the shared policy instead of owning a Web-only policy.
- Added `capability-authorization` audit records for capability decisions.

## Boundaries

- This slice does not change desktop/Electron.
- This slice does not solve missing OCR/vision/imagegen provider credentials; S6 owns that workflow closure.
- This slice does not make general shell commands default-allowed. General shell still requires prompt/full-access, while file-like workspace actions use the filesystem profile.
- Foreground user-initiated image job start is allowed outside read-only mode because output is routed through the existing image job service and controlled artifact path. Provider availability and credentials are still validated by the image generation runtime.
- Feishu `download`/`export` style read commands are only allowed when their local output path passes the filesystem write profile.
- Feishu `install`, `config_init`, `auth_login`, `agent_auth`, and `authorize_agent` are not read-only operations and cannot use the old structured default allow path.
- Web external-connection Feishu auth actions cannot call `FeishuCli.execute()` until the capability broker allows `feishu_cli:agent_auth`.
- Model-supplied `bash` arguments cannot downgrade shell execution into `workspace_read`/`workspace_write`; AgentStream treats bash as `system_shell`.
- Legacy broker fallback is only for brokers without `authorize_capability()`; malformed capability decisions are treated as hard denies.
- Image job parallelism policy remains Web-visible through runtime projection metadata, but the policy source of truth is no longer the Web handler.

## Acceptance

- Low-risk observability actions work in `smart-ask`, `read-only`, and `full-access`.
- Low-risk observability matrix is tested across `smart-ask`, `read-only`, and `full-access`.
- High-risk scheduler mutations are blocked in `read-only`.
- Feishu business reads pass without prompt; Feishu send/delete/permission/admin commands do not.
- Feishu install/config/auth structured actions respect read-only/smart-ask/full-access modes.
- Bash system shell is not silently allowed in `smart-ask`.
- Web status APIs cannot bypass the broker.
- Web status APIs have explicit deny-path tests proving broker refusal stops scheduler projection and image job service reads.
- Web external-connection Feishu auth has a deny-path test proving broker refusal stops direct CLI execution.
- Web file, artifact, project, and open-path helpers cannot treat malformed authorization results as allow.
- Tests and syntax checks pass with S5 artifacts recorded.

## Evidence

- `docs/web-runtime-goal/artifacts/S05-permission-guardrails-tests.json`
- `docs/web-runtime-goal/reviews/S05-consensus.md`

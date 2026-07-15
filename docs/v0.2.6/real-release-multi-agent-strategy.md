# EcoreX 真实发布多 Agent 分工策略

## 结论

可行，但只适合“并发分工取证 + 最终单次完整门禁”的模式。

不要让多个 Agent 同时运行完整 `python scripts/真实发布校验.py`。完整门禁会连接生产服务器，并触发真实模型、生图、改图和并发压力测试；重复运行会制造生产噪声、资源竞争和状态污染。

推荐命令：

```powershell
python scripts/真实发布多Agent分工策略.py
```

该命令只生成分工计划，不访问生产、不调用模型、不压测。

## 推荐时机

- 日常开发：只跑 `python scripts/真实发布轻量校验.py`。
- 版本冻结后：生成多 Agent 分工策略，让多个 Agent 并行做取证和修复。
- 候选包部署到生产后、推广前：先跑多 Agent 分工取证，再跑一次完整 `python scripts/真实发布校验.py`。
- 完整门禁通过前，不允许宣布真实发布验收通过。

## 并发波次

| 波次 | Lane | 是否并发 | 说明 |
| --- | --- | --- | --- |
| 0 | coordinator-light-preflight | 否 | 本地轻量校验、矩阵、文档、包装入口 |
| 1 | fresh-runtime-auth / ui-context-session / security-observability | 是 | 低成本线上证据，互不抢模型和压测资源 |
| 2 | stream-state-machine / tool-skill-mcp-cli / multi-model-image-route | 限流并发 | 涉及模型、工具链、生图/改图路由，必须使用独立 run id |
| 3 | concurrency-pressure | 否 | 压测独占，必须等待模型/工具/流式证据完成 |
| 4 | coordinator-final-real-release-gate | 否 | 唯一发布阻断结论，运行完整真实发布校验 |

## 硬规则

- 每个 Agent 必须使用唯一 `runId`、session、project 标记，避免会话串扰。
- 自动记忆不能作为能力通过证据；必须使用直接 API、SkillService、MCP 状态、EcoreX CLI、SSE ledger、route evidence。
- 生图和改图必须证明切换 OpenAI/DeepSeek/Gemini/Doubao 后仍走 `gpt-image-2-pro` native route。
- 压测必须独占运行，结束后 active request 必须清零。
- 所有 artifact 必须 redacted，不允许泄露 password、token、API key、raw URL、raw host、用户绝对路径。
- 多 Agent 分片报告只能辅助定位问题，最终发布结论只看 `docs/v0.2.6/artifacts/production-agent-product-acceptance.json`。

## 推荐分工

- Coordinator Agent：跑轻量校验，确认矩阵和文档标准，分配 lane，汇总 artifact。
- Fresh Runtime Agent：新用户、新机器、新环境、登录、runtime API、terminal state。
- UX Session Agent：前端 UI、移动/桌面、项目会话、上下文、自动/手动压缩。
- Security Observability Agent：redaction、诊断包、日志、admin gate、发布 artifact。
- State Machine Agent：SSE、刷新恢复、取消、重连 replay、run ledger、UI pending 状态。
- Toolchain Agent：Skill/MCP/CLI、OCR、Vision、browser、file、Office/PDF。
- Model Route Agent：多模型切换、生图/改图路由、`gpt-image-2-pro` 不漂移。
- Pressure Agent：20 虚拟用户、60 请求、P95、资源回落、stuck request 清零。

## 合并规则

- 任一 P0 lane 失败，先修复，不进入完整门禁。
- 任一 artifact 出现 redaction 失败，先删除或重生成该 artifact。
- 压测后 active request 不为零，必须清理并重跑压测 lane。
- 所有分工取证完成后，再由 Coordinator Agent 询问用户是否运行完整真实发布校验。

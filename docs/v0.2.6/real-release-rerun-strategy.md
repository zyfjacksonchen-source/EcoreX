# EcoreX 真实发布失败后复验策略

## 结论

测试失败后，不建议修一个问题就完整跑一遍 `python scripts/真实发布校验.py`。

推荐策略是：

1. 保留失败报告，不覆盖原始 artifact。
2. 生成失败复验计划。
3. 修复后先跑轻量校验。
4. 只复验失败 group 和必要依赖 group。
5. 所有 P0/P1 focused rerun 通过后，最后只跑一次完整真实发布校验。

## 命令

根据上一次完整门禁报告生成复验计划：

```powershell
python scripts/真实发布失败复验策略.py
```

没有报告时，也可以手动指定 group：

```powershell
python scripts/真实发布失败复验策略.py --groups stream-state-machine,context-session
```

修复后的 focused rerun 示例：

```powershell
python scripts/真实发布校验.py --focus-groups stream-state-machine,context-session --skip-legacy --output docs/v0.2.6/artifacts/real-release-focused-rerun.json
```

最后发布前仍必须跑完整门禁：

```powershell
python scripts/真实发布校验.py
```

## 复验分层

| 场景 | 修复后先跑 | 是否马上完整跑 |
| --- | --- | --- |
| 文档、矩阵、包装入口 | `真实发布轻量校验.py` | 否 |
| UI、会话、上下文 | focused rerun 对应 group | 否，批量后再跑 |
| SSE、状态机、取消、刷新恢复 | focused rerun + 相关依赖 | 否，批量后再跑 |
| Skill/MCP/CLI/OCR/Vision | focused rerun 对应 group | 否，批量后再跑 |
| 多模型切换、生图/改图路由 | focused rerun `multi-model-image-route` | 否，但发布前必须完整跑 |
| 并发压力、active request 不清零 | 串行 focused pressure rerun | 否，但不能与其他压测并行 |
| redaction/security 失败 | focused rerun + artifact 重新生成 | 否，但必须确认旧泄露 artifact 已清理 |

## 硬规则

- focused rerun 不是发布结论，只是 proof-of-fix 证据。
- focused rerun 必须使用新的 run id，不能借助自动记忆证明能力。
- 同一问题 focused rerun 连续失败 2 次后，不要继续盲目重跑；应转入日志、ledger、artifact 级定位。
- 压测复验必须串行独占。
- 最终发布结论只来自 `docs/v0.2.6/artifacts/production-agent-product-acceptance.json`。
- 完整真实发布校验仍然只在“部署后、推广前”运行，并且运行前应询问用户确认。

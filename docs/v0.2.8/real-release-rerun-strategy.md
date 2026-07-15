# EcoreX v0.2.8 真实发布失败复验策略

## 结论

测试失败后，不要每修一个问题就完整跑一遍 `python scripts/真实发布校验.py`。先保留失败报告，运行 `python scripts/真实发布失败复验策略.py` 生成 focused rerun 计划，修复后跑 `python scripts/真实发布轻量校验.py` 和失败 group 的依赖链复验。

## v0.2.8 复验重点

- 同会话消息仍中止当前任务：复验 `v028-runtime-observability-queue`，它会带上 `fresh-env`、`auth-first-use`、`runtime-api` 依赖。
- 长任务无观测或 image job 无干预动作：复验 `v028-runtime-observability-queue`，并检查 `task_observations`、Run Center 观测行和 image-job `continue`/`extend`/`background`。
- queued run 重启后丢失或重复启动：复验 `v028-runtime-observability-queue`，重点看 queued payload store 与 RunLedger claim lease。
- custom Gemini、imagegen 多图、Tongxin MPI、CDP/OCR/Vision 失败：继续复验 `v027-integrated-capabilities` 与相关依赖。

## 命令

```powershell
python scripts/真实发布失败复验策略.py
python scripts/真实发布校验.py --focus-groups v028-runtime-observability-queue --skip-legacy --output docs/v0.2.8/artifacts/real-release-focused-v028-runtime-observability-queue.json
python scripts/真实发布校验.py
```

最终发布结论只来自 `docs/v0.2.8/artifacts/production-agent-product-acceptance.json`。完整真实发布校验仍然只在“部署后、推广前”运行，并且运行前应主动询问用户是否运行真实发布校验。

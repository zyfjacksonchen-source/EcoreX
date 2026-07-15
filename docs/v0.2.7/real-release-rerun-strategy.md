# EcoreX v0.2.7 真实发布失败复验策略

## 结论

测试失败后，不要每修一个问题就完整跑一遍 `python scripts/真实发布校验.py`。先保留失败报告，运行 `python scripts/真实发布失败复验策略.py` 生成 focused rerun 计划，修复后跑 `python scripts/真实发布轻量校验.py` 和失败 group 的依赖链复验。

## v0.2.7 复验重点

- custom Gemini 或切模型失忆失败：复验 `v027-integrated-capabilities`，它会带上 `fresh-env`、`auth-first-use`、`stream-state-machine`、`context-session`、`tool-skill` 依赖。
- imagegen 多图或 shell fallback 失败：复验 `multi-model-image-route` 和 `v027-integrated-capabilities`。
- Tongxin MPI 失败：复验 `v027-integrated-capabilities`，MPI 不可达、样本为 0、cache fallback 冒充 MPI 都阻断。
- CDP/OCR/Vision 或 mac runtime parity 失败：先跑轻量校验，再跑对应 focused rerun。

## 命令

```powershell
python scripts/真实发布失败复验策略.py
python scripts/真实发布校验.py --focus-groups v027-integrated-capabilities --skip-legacy --output docs/v0.2.7/artifacts/real-release-focused-rerun.json
python scripts/真实发布校验.py
```

最终发布结论只来自 `docs/v0.2.7/artifacts/production-agent-product-acceptance.json`。完整真实发布校验仍然只在“部署后、推广前”运行，并且运行前应主动询问用户是否运行真实发布校验。

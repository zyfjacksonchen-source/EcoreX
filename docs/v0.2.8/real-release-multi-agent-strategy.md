# EcoreX v0.2.8 真实发布多 Agent 分工策略

## 结论

可行，但只适合“并发分工取证 + 最终单次完整门禁”的模式。

日常开发先跑 `python scripts/真实发布轻量校验.py`。版本冻结和候选包部署后，先用 `python scripts/真实发布多Agent分工策略.py` 生成并发多 Agent 分工策略，再由多个 Agent 分 lane 取证。生产候选包部署到真实服务器之后、正式推广前，最终只跑一次完整 `python scripts/真实发布校验.py`。

## v0.2.8 新增 Lane

`agent-i-v028-runtime-observability-queue` 覆盖 Codex 风格同会话 queue-first、queued payload 持久化、RunLedger claim lease、`task_observations` projection、image-job `continue`/`extend`/`background` 干预动作，以及 Run Center 观测面。

## 硬规则

- 完整真实发布校验不可并发运行。
- focused rerun 只是 proof-of-fix 证据，不是发布结论。
- 最终发布结论仍必须由单次完整 `python scripts/真实发布校验.py` 产出。
- 开发 Agent 在部署后、推广前应主动询问用户是否运行真实发布校验。
- 公开 artifact 只保留 hash、计数、类别、漂移区间和阈值，不保留 raw URL、raw host、用户绝对路径、密钥或敏感正文。

## 推荐入口

```powershell
python scripts/真实发布轻量校验.py
python scripts/真实发布多Agent分工策略.py
python scripts/真实发布校验.py
```

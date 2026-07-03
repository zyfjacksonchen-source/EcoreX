# EcoreX v0.2.7 真实发布多 Agent 分工策略

## 结论

可行，但只适合“并发分工取证 + 最终单次完整门禁”的模式。

日常开发先跑 `python scripts/真实发布轻量校验.py`。版本冻结和候选包部署后，先用 `python scripts/真实发布多Agent分工策略.py` 生成并发多 Agent 分工策略，再由多个 Agent 分 lane 取证。生产候选包部署到真实服务器之后、正式推广前，最终只跑一次完整 `python scripts/真实发布校验.py`。

## v0.2.7 新增 Lane

`agent-h-v027-integrated-capabilities` 覆盖 custom Gemini、同会话切模型上下文连续性、`model-switch-divider` 分页分隔线、CDP-first、Vision/OCR、本地文件上下文性能、macOS runtime parity、imagegen 多图路由、Tongxin MPI 准确性对照。

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

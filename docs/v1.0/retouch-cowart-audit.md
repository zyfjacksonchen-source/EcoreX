# EcoreX v1 精准修图与 Cowart clean-room 对照

审计日期：2026-07-11
参考基线：`zhongerxin/Cowart@61f6daaf4de0f2cbab47008f49ca9dda9e8e1819`

本记录只对照公开行为和数据边界，不复制 Cowart 的 tldraw UI、源码或
Codex 专用 Widget/MCP 实现。EcoreX 是 Web-only 办公 Agent，运行权威仍在
本地 Python Runtime。

## 已吸收的产品方法

| Cowart 可验证方法 | EcoreX v1 落点 | EcoreX 产品化约束 |
|---|---|---|
| 在原图上创建有说明的区域标注 | `RetouchWorkspace.tsx` 的点、矩形、椭圆、多边形、折线和画笔 | 坐标统一为定向后的 `0..1` 归一化坐标；前端不拼 imagegen prompt |
| 修改结果不覆盖原图，便于前后比较 | Artifact 新 revision、原图/新修订/并排对比 | 原 revision 与 lineage 永久保留；结果直接回到消息和 Artifact 投影 |
| 参考图随生成请求提交 | `reference_artifact_ids` 与冻结 revision 快照 | 只接受同账户、后端可见且摘要已验证的图片 Artifact；最多 10 张 |
| 画布选择、视口和草稿可恢复 | 后端 `RetouchWorkspaceProjection`、expected-version 保存与重开 | 草稿、标注、视口、引用和整体说明均持久化；冲突保留本地修改并显式提示 |
| 标注需要形成模型可理解的视觉约束 | `retouch_surface.compile_annotation_mask` | 后端确定性编译有界 PNG mask 和 pixel regions；标注层为 internal Artifact |
| 生成后检查并继续修改 | change summary、inspection regions、继续修改/打开/对比 | 新结果必须带质量证据和检查区域，继续修改从新 revision 开始 |
| 无行为的按钮不应存在 | 精修工作区所有 Button/IconButton 都绑定实际命令 | 菜单、图片来源或“更多”没有真实合同则不渲染 |

## EcoreX 不沿用的部分

- 不引入无限画布或 tldraw。精准修图围绕一个冻结 Artifact revision，避免
  办公用户先理解图形编辑器的数据模型。
- 不让 Skill/MCP 直接写画布 JSON、路径或图片资产。所有写入通过
  Artifact、RetouchJob、CAS 和后端策略合同。
- 不把“带标注的整张界面截图”作为唯一编辑输入。EcoreX 传原始像素、
  结构化标注、确定性 mask 和参考图；工具栏、选框和文字标注不会污染结果。
- 不使用时间戳作为图片身份。显示名只到分钟，真实身份由 ULID、revision
  和 SHA-256 组成。

## 已有阻断测试

- `tests/v1/test_retouch_workspace_contract.py`：画布重启恢复、版本冲突、全部
  几何类型、定向坐标、mask 摘要和完成结果表面。
- `tests/v1/test_retouch_execution.py`：冻结输入 revision、租约/心跳、
  recover-before-resubmit、结果 staging、重试、取消和 internal 数据隔离。
- `tests/v1/test_runtime_retouch_integration.py`：真实 `/api/v1` 结构化命令，
  不接受 prompt 路由或客户端路径。
- `desktop/src/v1/state/retouchCanvas.test.ts` 与
  `retouchPresentation.test.ts`：坐标、命中、移动、撤销重做和结果呈现。

## 仍需真实环境取证

代码与确定性测试不能替代真实托管图片 Provider 的视觉质量。GA 候选仍需用
同一批原图覆盖文字替换、局部物体删除、背景替换、低对比边缘、EXIF 旋转、
超宽图和多参考图，验证输出不含标注痕迹、未修改区域保持稳定，并记录模型、
请求摘要、结果摘要、耗时和人工验收结论。

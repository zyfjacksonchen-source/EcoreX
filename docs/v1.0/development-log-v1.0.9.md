# EcoreX v1.0.9 开发与验收留痕

更新时间：2026-07-22（Asia/Shanghai）

## 本轮根因与闭环

- 安装器：已安装旧 v1 可通过同一签名 Bootstrap 进入标准更新事务，不再把新 manifest 判为无关 Runtime；保留 side-by-side slot 与用户数据。
- 更新发现：Runtime 使用生产 `/api/v1/releases/latest` 与 `/api/v1/client/updates/ws`；横幅按不可变 release/build 身份关闭，不再随下载状态反复弹出。
- 输入附件：上传、就绪态、发送后缩略图、鉴权 Blob、完整适配预览已接通；每 Turn 最多 20 个文件，其中图片最多 4 张，前后端共同约束。
- 多模态：上传图片经 EXIF 校正、40MP 像素上限、2048px 最大边和有界 JPEG rendition 后进入模型；源 SHA 与 rendition SHA 双追溯，原件 CAS 不变。
- OCR/Vision：只读取当前 Turn 绑定图片；OCR 与 Vision 辅助 OCR 使用受控 rendition，避免原始大图导致内存峰值。
- 生图/修图：`imagegen` 支持当前 Turn 的 `attachment_ids`，上传图片可以直接成为修图输入；图片执行使用独立有界池（默认并发 2、等待 8、超时 900 秒），不占满普通办公 Turn worker。
- 工具发现：显式 shell/bash/read/fetch/imagegen/OCR 等只提升对应能力，不排除其他工具；图片附件会披露 read/vision/OCR，未知能力保持 fail-closed。
- 项目会话：后端按精确 Job/Turn/Thread/Project 解析项目根；read 相对路径、Pack cwd 和沙箱根以项目为先，跨项目、伪造作用域、删除或链接替换均拒绝。
- 模型目录：每个新 Turn 刷新当前签名 allowlist 并固化新模型快照；前端同步撤销失效选择。活动 Turn 中更换模型自动改为“排队”，避免 UI 显示已切换而 steer 仍使用旧模型。
- UI：附件上传/就绪反馈、发送后缩略图、点击全图适配预览、居中圆形“回到底部”、紧凑连接器和菜单密度已完成。

## 已执行验证

- Runtime/Worker/项目/Pack 聚合：63 passed。
- 附件/多模态/修图聚合：20 passed。
- 完整关键后端聚合：230 passed、1 skipped；并发执行时两个 Windows 主机环境测试发生临时 durable rename 拒绝，顺序复跑 2 passed。
- Windows AppContainer native probe 在当前受限执行宿主内返回 restricted-token 环境错误；必须在签名安装包的真实本机验收阶段复验，未据此声明通过。
- Web RuntimeClient、模型、语言、密度、Timeline：71 passed。
- TypeScript `--noEmit`：通过。
- Ruff 与 Python 编译：通过。

## 发布阻断条件

只有以下真实安装验收全部通过后才允许推送 stable：固定安装根版本/摘要、桌面快捷方式、生图、上传图修图、OCR、语义视觉、shell/read/fetch、项目工作区、并发生图期间普通对话、模型切换、缩略图与完整预览。发布后还需对公开下载页、管理员端、release feed 和安装命令做外部回读。

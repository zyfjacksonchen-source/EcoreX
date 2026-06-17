# EcoreX v0.1.14 Acceptance Checklist

## 开工与回滚

- [ ] 开工前已在 v0.1.13 基线做 checkpoint commit。
- [ ] 开发分支为 `codex/ecorex-v0.1.14`。
- [ ] `.tmp-*`、`release-local-*`、缓存目录和未确认打包产物未纳入提交。

## 构建与基础 Smoke

- [ ] `npm --prefix desktop run typecheck` 通过。
- [ ] `npm --prefix desktop run build` 通过。
- [ ] Python 单测覆盖 update-check、ui-state、subagent、agent_capability、open-path、knowledge route、stale lock cleanup。
- [ ] Desktop 启动后 sidecar 状态从 starting 进入 running。
- [ ] sidecar 异常退出 code 0 时会自动恢复，不长期停留在“等待运行时”。

## 更新与数据保留

- [ ] Windows v0.1.13 已安装客户端能从本地 feed 检测 v0.1.14。
- [ ] Windows 自动下载更新，但不强制安装。
- [ ] 有 active request 时点击安装更新被阻止，并显示原因。
- [ ] macOS 只提醒新版本并跳转下载页，不自动替换本地 app。
- [ ] WebUI 只提示下载新版，不自动覆盖本地目录。
- [ ] 升级后 session、聊天文本、message extras、tool 记录、上传文件、pasted files、生成产物、active session/project 不丢失。

## Agent 安装能力包

- [ ] 能力页点击安装后，在当前会话生成 agent 安装任务。
- [ ] UI 不再调用应用侧 `installCapabilityPack` 直装能力包。
- [ ] 启动时不再由 Electron main 进程自动预装能力包。
- [ ] 安装任务使用 `agent_capability install_pack`。
- [ ] 权限确认走工具权限弹窗，不要求用户在聊天里输入“同意安装”。
- [ ] 点击确认后不出现“该会话正在执行中，请稍后再试”。
- [ ] 安装中弹窗显示“xx 正在安装，请稍后”，可关闭，安装继续。
- [ ] 安装中同一能力按钮禁用并显示 installing 状态。
- [ ] 成功后弹出成功提醒，能力状态刷新为已安装。
- [ ] 失败后 agent 读取诊断、stdout/stderr、log path，并给出修复动作。
- [ ] read-only 或 admin disabled 下安装被阻止并记录审计。

## Subagent v1

- [ ] `subagent start/status/list/collect/cancel` 可用。
- [ ] 默认角色为 explorer，可显式指定 default/worker/explorer。
- [ ] 子任务独立 child session，继承父会话 workspace、模型、权限、quota。
- [ ] 最大并发 6 被正确限制。
- [ ] 最大深度 1 被正确限制。
- [ ] 父任务 cancel 时子任务可被取消或进入可收敛状态。
- [ ] CSV 批量、递归子代理暂列 v1 后续补全项，不阻塞 v0.1.14。

## 会话状态与完成态

- [ ] 左侧运行中会话显示小型 spinner，不再误落到完成态。
- [ ] 非当前会话输出完成后保留未读小点，点进会话后清除。
- [ ] 等待权限、能力安装确认、打开文件确认时显示橙色小字“等待回复”。
- [ ] 用户确认、拒绝或取消后等待状态清除。
- [ ] 任务完成后展示结论和产物总结。
- [ ] 工具调用过程默认折叠，可展开查看。

## WebUI、知识库与产物

- [ ] 默认生图走 `gpt-image-2-pro`，不可用时才 fallback 到 `gpt-image-2`。
- [ ] agent 不用 HTML/canvas/SVG/Pillow 等编码方式冒充最终生成图片。
- [ ] WebUI 打开项目文件夹走 `/api/open-path`，不误走 `/api/file`。
- [ ] `/app/knowledge/<path>.md` 进入知识库 viewer，不出现裸 `not found`。
- [ ] Desktop/WebUI 知识库链接行为一致。
- [ ] 文件不存在或权限不足时显示应用内错误。
- [ ] agent 产物默认优先打开本地 `file_path`，没有本地文件时才使用 URL。
- [ ] 生成图片在聊天内直接预览，不只展示路径。
- [ ] 单图、多图、中文路径、空格路径、相对路径、绝对路径均可预览。
- [ ] 图片仍保留复制路径、下载或打开本地文件入口。

## 内部提示词与发布包卫生

- [ ] tool-chain/convergence/system guidance 不进入用户可见 content、tool result、phase、assistant message。
- [ ] 前端 render、copy、history restore、long answer、tool detail 都走同一 redaction。
- [ ] 重复工具链触发时用户只看到本地化状态文案。
- [ ] WebUI/桌面发布包内不能包含 `CowAgent`、`COWAGENT`、`cowagent`、`C:\CowAgent`、旧仓库 URL 等可解析痕迹。
- [ ] release runtime staging 后执行 sanitizer/validator。

## 下载页与 Admin

- [ ] 下载页根据访问者系统自动推荐 Windows 或 macOS 下载。
- [ ] Windows 推荐 Windows Desktop installer 和 Windows WebUI 包。
- [ ] macOS 推荐 macOS Desktop DMG 和 macOS WebUI 包。
- [ ] macOS 架构不可可靠识别时保留 Apple Silicon/Intel 选择器。
- [ ] v0.1.13 管理后台测试 v0.1.14 时，Desktop/WebUI 默认先用 v0.1.14 client key；若后台返回 `invalid client key`，客户端自动 fallback 到 v0.1.13 key，不再把该错误暴露给用户。
- [ ] 自定义企业 client key 不自动 fallback 到公开默认 key；自定义 `compatClientEventKeys`/`ECOREX_WEB_CLIENT_KEYS` 只使用显式配置的兼容 key。
- [ ] 公网 manifest、release notes、下载页版本均为 0.1.14。

## 发布前线上验证

- [ ] 上传 installer、blockmap、latest.yml、DMG、WebUI 包。
- [ ] 更新 public manifest 和下载页。
- [ ] admin API/Web 服务升级到 0.1.14。
- [ ] 真实 v0.1.13 Windows 客户端连公网 feed 可升级。
- [ ] 真实 macOS/WebUI 仅提示下载，并能打开正确下载页面。
- [ ] 用户手测通过后再发布部署、最终 commit/push。

## 本地自动验证记录

- [x] 2026-06-17 checkpoint commit 已完成：`cdd685a chore: checkpoint v0.1.13 release baseline`。
- [x] 2026-06-17 当前开发分支：`codex/ecorex-v0.1.14`。
- [x] 2026-06-17 `npm --prefix desktop run typecheck` 通过。
- [x] 2026-06-17 `npm --prefix desktop run build` 通过，当前 bundle 为 `index-Djz3VgT_.js` / `index-DFNL8MON.css`。
- [x] 2026-06-17 `python -m unittest tests.test_ecorex_web_parallel_backend` 通过，91 tests OK。
- [x] 2026-06-17 `desktop/scripts/smoke-renderer-visual.ps1` 通过，截图输出到 `C:\CowAgent\tmp\ecorex-desktop-visual`。
- [x] 2026-06-17 `python scripts/validate-ecorex-release-artifacts.py --desktop-only --desktop-dir desktop/release/win-unpacked` 通过。
- [x] 2026-06-17 WebUI 本地包 `EcoreX_0.1.14-webui-windows-x64.zip`、`EcoreX_0.1.14-webui-macos-universal.zip`、`EcoreX_0.1.14-webui-win-mac.zip` 通过 release validator zip 扫描。
- [x] 2026-06-17 release sanitizer/validator 扫描发布 runtime、Desktop asar、WebUI zip，未发现 `CowAgent`、`COWAGENT`、`cowagent`、`C:\CowAgent`、`C:/CowAgent` 可见残留。
- [x] 2026-06-17 Desktop enterprise client key fallback 本地脚本通过：默认 `ecorex-desktop-v0.1.14`，`invalid client key` 场景可兼容 `ecorex-desktop-v0.1.13`；自定义 key 不 fallback 到公开默认 key。
- [x] 2026-06-17 已重新执行 `npm --prefix desktop run stage:runtime:win` 和本地 `electron-builder --dir`，刷新 `C:\CowAgent\desktop\release\win-unpacked`；刷新后 Desktop validator 和 runtime sanitizer 通过。
- [x] 2026-06-17 已生成本地未签名 Windows update feed：`C:\CowAgent\desktop\release\EcoreX_0.1.14_x64-setup.exe`、`.blockmap`、`latest.yml`；`latest.yml` 的 version、size、sha512 已与 exe 校验一致，签名状态为 `NotSigned`，仅用于本地手测。
- [x] 2026-06-17 Desktop updater 支持 `enterprise-policy.json` / 环境变量覆盖本地测试地址：`updateFeedUrl`、`updateManifestUrl`、`downloadPageUrl`，默认公网地址不变。
- [x] 2026-06-17 Windows 本地更新手测说明已记录：`docs/ecorex/v0.1.14/windows-update-local-handtest.md`。
- [x] 2026-06-17 已生成本地 Web/Linux 服务包：`C:\CowAgent\release-artifacts\EcoreX_0.1.14-web-linux-service.tar.gz`，大小 `3157449`，SHA256 `DBCD33853359F88569A49DA6BB2749DFDE6A4AE696D2F1D2328F2A6C2E4B0E8B`；tar validator 和 runtime sanitizer 通过。
- [x] 2026-06-17 下载页本地 Playwright 模拟通过：Windows 首推 Desktop + Windows WebUI，macOS Intel 首推 Intel DMG + macOS WebUI，macOS Apple Silicon 首推 arm64 DMG + macOS WebUI，未知设备保留通用顺序和 macOS 架构选择器。
- [x] 2026-06-17 release validator 已增加 public site 回归检查，防止 macOS 架构选择器再次只在 `ready()` 状态下才选中设备推荐版本。
- [x] 2026-06-17 修复 `electron-updater` CommonJS 导入导致的主进程 JavaScript error；刷新 `win-unpacked` 后 typecheck/build、Desktop validator、runtime sanitizer 通过，并重新打开本地手测版。
- [x] 2026-06-17 修复手测反馈：隐藏用户可见“已暂停，输入新消息后继续”状态；SSE 重连失败后保持待恢复 pending 而不是清 request；左侧运行中显示 spinner、完成未读显示更大的静态橙点；更新检查的 `app-update.yml`/`ENOENT` 在 unpacked 测试包中降级为“当前测试包未配置自动更新通道”。
- [x] 2026-06-17 同步 `channel/web/static/app` 和 `desktop/runtime/ecorex-runtime/channel/web/static/app` 到当前前端 bundle，重新生成 `win-unpacked`；Desktop validator、runtime sanitizer、发布前端/updater 文本扫描均通过，旧 WebUI hash、旧暂停文案、完整英文内部 tool-chain 提示词未再明文出现。
- [x] 2026-06-17 默认生图模型修正为 `gpt-image-2-pro`，OpenAI 请求前兼容归一化旧 `image-2-pro` / `image-2` 别名；无输入图片走 `/images/generations`，有参考/编辑图片走 `/images/edits`。
- [x] 2026-06-17 聊天内产物展示支持从本地交付目录和文件列表中提取 PNG/JPG/WebP/PDF/MD/JSON 等文件，渲染为可点击产物卡片，优先通过本地路径打开。
- [x] 2026-06-17 `npm --prefix desktop run typecheck`、`npm --prefix desktop run build`、`python -m unittest tests.test_ecorex_web_parallel_backend` 均已在最终修复后重新通过；当前前端 bundle 为 `index-BIk2zuoP.js` / `index-Co1SbVyo.css`。
- [x] 2026-06-17 最终本地 Windows 包已重新打包：`desktop/release/EcoreX_0.1.14_x64-setup.exe`，大小 `149411167`，生成时间 `2026-06-17 17:32`；对应 `.blockmap` 已生成。
- [x] 2026-06-17 最终本地 `desktop/release/win-unpacked` 通过 Desktop validator、runtime sanitizer 和静态文本扫描，未发现旧 OpenAI 生图默认值、旧 `autoUpdater` 导入错误、旧暂停文案或内部 tool-chain 提示词明文。
- [x] 2026-06-17 已从 `desktop/release/win-unpacked/EcoreX.exe` 打开最终本地发布包供用户手测；该包尚未发布部署、尚未最终 commit/push。
- [x] 2026-06-17 手测反馈修复：`latest.yml` 公网 404 不再展示 electron-updater 原始 `HttpError` / `app.asar` 堆栈，降级为中文短提示并可打开下载页。
- [x] 2026-06-17 手测反馈修复：生图默认链路继续锁定 `gpt-image-2-pro`；OpenAI 与 LinkAI 设置页模型列表均以 `gpt-image-2-pro` 为首选，`image-2-pro` 仅作为兼容别名输入。
- [x] 2026-06-17 手测反馈修复：发送消息后立即插入用户消息和 assistant pending 首帧，企业额度检查改到首帧之后执行，额度检查异常不阻断发送主流程。
- [x] 2026-06-17 最终手测包已再次重新打包并打开：`desktop/release/EcoreX_0.1.14_x64-setup.exe`，大小 `149412009`，生成时间约 `2026-06-17 18:18`；当前前端 bundle 为 `index-DvvyXXS_.js` / `index-Co1SbVyo.css`。
- [x] 2026-06-17 最新标准包通过 `npm --prefix desktop run typecheck`、目标后端单测、`npm --prefix desktop run build`、`stage:runtime:win`、Desktop validator、runtime sanitizer；重复启动仍只有 1 个 sidecar，`/api/models` 生图 fallback 为 `openai / gpt-image-2-pro`，LinkAI 列表首项为 `gpt-image-2-pro`。
- [x] 2026-06-17 标准包手测日志确认：封面和 4 张轮播内页均调用 `gpt-image-2-pro` 成功生成并通过 `send` 工具进入图片发送流程；用户中断第 5 张后新消息可继续执行，`/api/active-requests` 最终为空，无残留运行锁。
- [x] 2026-06-17 用户手测确认：多图产物以缩略图网格展示的体验通过，保留图片文件名和本地打开入口。
- [x] 2026-06-17 手测后补充修复：发布包归属元数据已更新为 EcoreX / `zhangyifanjackson-dotcom`；`LICENSE`、`pyproject.toml`、asar `package.json` 描述不再暴露旧项目名/旧 GitHub 名；release sanitizer 和 validator 新增 `zhayujie` / `chatgpt-on-wechat` / `chatgpt_on_wechat` 阻断。
- [x] 2026-06-17 新本地 Windows 包重新打包：`desktop/release/EcoreX_0.1.14_x64-setup.exe`，大小 `149411853`；完整 `tests.test_ecorex_web_parallel_backend` 91 项、`npm --prefix desktop run typecheck`、`npm --prefix desktop run build`、Desktop validator、runtime sanitizer、asar 元数据检查均通过。
- [x] 2026-06-17 public release 本地包已生成并通过 validator：`release-artifacts/EcoreX_0.1.14-public-release.zip`，大小 `392934980`，SHA256 `9641C49ED9FED62DEF4B8E8C273F4A6347323C40794E16652D90F056B24A4F80`。
- [x] 2026-06-17 public release 包的 `site/downloads/` 已包含 Windows installer、`latest.yml`、`.blockmap`、Windows WebUI、macOS WebUI、Linux web service；macOS Desktop DMG 继续保持 `pending-build`，未在 manifest 中伪造 ready。
- [x] 2026-06-17 public release validator 已校验 `latest.yml` 的 version、path、url、size 与 Windows installer 一致，避免 Windows 自动更新再次 404 或拿不到 update feed。
- [x] 2026-06-17 public release 安装脚本本地等价 smoke 通过：`install-ecorex-public-release.sh` 可将 zip 安装到临时 `release/current` 结构，`check-ecorex-server-release.sh` 在 `CHECK_PUBLIC=0 CHECK_CADDY=0` 下通过 ready artifact 校验。
- [x] 2026-06-17 GitHub 源码提交已在本地创建，提交信息为 `Implement EcoreX v0.1.14 release`；同时生成离线交付备份 `release-artifacts/EcoreX_0.1.14-source.bundle` 和 `release-artifacts/EcoreX_0.1.14-source.patch`。
- [x] 2026-06-17 public 下载页已部署到生产：`https://www.ecoreai.cn/ecorex-agent/manifest.json` 返回 `0.1.14`，`/srv/ecorex-agent-download/current` 指向 `/srv/ecorex-agent-download/releases/20260617112343-v0.1.14`。
- [x] 2026-06-17 生产 downloads 已验证：Windows installer、`.blockmap`、`latest.yml`、Windows WebUI、macOS WebUI、Linux web service 均 HTTP 200，大小与 manifest/checksum 一致。
- [x] 2026-06-17 生产 WebUI runtime 已升级：`/opt/ecorex-web/current` 指向 `/opt/ecorex-web/releases/20260617112511-v0.1.14`，`https://www.ecoreai.cn/ecorex-agent/api/version` 返回 `0.1.14`，`/app/` 服务 `index-DvvyXXS_.js`。
- [x] 2026-06-17 生产 Admin API 已重建并重启 Docker Compose 服务 `xhs-report-ecorex-admin-api-1`，v0.1.14 desktop/web client key 均返回 `401 missing user token`，不再是 `403 invalid client key`，v0.1.13 key 兼容保留。
- [x] 2026-06-17 生产 server checks 通过：`check-ecorex-server-release.sh` 通过 public manifest/root/assets/admin gate/client gate 和 ready artifact HTTP 检查；`check-ecorex-web-release.sh` 使用 `BASE_URL=http://172.18.0.1:9909` 通过 login/app/auth/version/SSE 检查。
- [ ] 待用户手测：真实 v0.1.13 Windows 已安装客户端升级到 v0.1.14 后 session、聊天文本、附件、图片产物、active session/project 不丢失。
- [ ] 待用户手测：macOS/WebUI 真实环境只提示下载，不自动覆盖本地目录。
- [ ] 待用户手测：能力安装在当前会话内由 agent 执行，安装提示可关闭且完成后弹出成功/失败提醒。
- [ ] 待用户手测：WebUI 项目文件夹、知识库链接、图片产物聊天内预览、左侧会话小点/等待回复状态均符合预期。
- [ ] 待用户确认后再发布部署、最终 commit/push。

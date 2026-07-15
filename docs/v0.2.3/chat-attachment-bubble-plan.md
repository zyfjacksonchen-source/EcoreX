# R23-21 Codex-like 用户附件聊天气泡整改计划

## 目标

修复用户消息同时包含文字、文件附件、图片附件时的视觉问题。当前 EcoreX 把正文和附件放进一个强橙色大气泡，附件卡片尺寸过大、边框过强；v0.2.3 要调整为 Codex-like 的轻量布局：附件紧凑地浮在正文上方，正文单独成柔和圆角气泡，仍保留 EcoreX 橙色主色。

## 范围

- 只调整聊天 transcript 中用户消息的附件展示和正文气泡。
- 不改变 composer 附件托盘、assistant artifact 卡片、文件打开/右键菜单、后端消息 schema。
- 不回退 v0.2.2 的文件预览、复制、上下文菜单、历史恢复能力。

## 目标样式

- 用户消息容器透明，不再用一个大橙框包住所有内容。
- 附件区域在正文气泡上方，右对齐，支持文件 pill 与图片 thumbnail 混排。
- 文件附件：紧凑 pill，显示文件图标和文件名，单行省略。
- 图片附件：小尺寸缩略图，隐藏冗余文件名，保持可点击打开。
- 正文气泡：轻量橙色 tint、细弱边框、圆角，复制按钮仍在气泡右上角。
- 窄屏下不溢出、不遮挡、不挤压正文。

## 已实施源码改动

- `desktop/src/App.tsx`
  - 用户消息附件提前渲染到正文气泡上方。
  - 新增 `message-text-bubble` / `message-content-shell` 分层。
  - `article.message` 增加 `has-files` 状态类。
- `desktop/src/styles/app.css`
  - 用户 `.message-body` 改为透明布局容器。
  - 新增轻量 `.message.user .message-text-bubble`。
  - 新增用户附件 pill/thumbnail 专属样式，降低边框强度和卡片尺寸。

## 验收门禁

- Frontend/UX：与 Codex-like 附件布局一致，仍保留 EcoreX 橙色主色。Nash 只读审查 PASS。
- Harness/Test：`npm --prefix desktop run typecheck`、`npm --prefix desktop run build:renderer`、`python scripts\smoke-chat-attachment-bubble-static.py`、`python scripts\smoke-chat-attachment-bubble-browser.py` 通过。静态 light/dark/narrow 与 built React app 历史消息附件链路均有证据。
- Regression：assistant artifact、composer attachment tray、文件右键菜单、复制按钮不回退。Galileo 只读审查 PASS。
- Accessibility：按钮仍有 title/aria，文件名长文本省略，不因缩略图隐藏文件名而失去打开能力。

## 必补产物

- `docs/v0.2.3/artifacts/chat-attachment-bubble-light.png`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-dark.png`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-narrow.png`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-smoke.json`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-browser.png`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-browser-smoke.json`
- `docs/v0.2.3/artifacts/chat-attachment-bubble-browser-privacy-scan.json`

## 当前状态

PASS。源码已按窄范围调整，三路多 agent 审查达成一致；随后补齐完整运行态 app/browser smoke，确认 built `desktop/dist` React app 可以从 `/api/history` 与 runtime attachment extras 渲染同一套 Codex-like 布局，且 Run Center 仍隐藏。

已通过命令：

- `npm --prefix desktop run typecheck`
- `npm --prefix desktop run build:renderer`
- `python -m py_compile scripts\smoke-chat-attachment-bubble-static.py`
- `python scripts\smoke-chat-attachment-bubble-static.py`
- `python scripts\smoke-chat-attachment-bubble-browser.py`
- `python scripts\scan-session-artifacts-privacy.py docs\v0.2.3\artifacts\chat-attachment-bubble-browser-smoke.json --json-output docs\v0.2.3\artifacts\chat-attachment-bubble-browser-privacy-scan.json --salt v023-chat-bubble-browser`

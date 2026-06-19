"""User-facing release notes exposed to desktop and WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.1.16",
    "title": "EcoreX 0.1.16 更新说明",
    "summary": "这次更新聚焦 Codex-like 桌面体验：更安静的消息流、更明确的产物披露、更可靠的本地文件打开和更稳的流式生命周期。",
    "highlights": [
        "AI 回复去掉卡片阴影，改为更接近 Codex 桌面端的正文排版与低噪声过程披露。",
        "流式 Markdown 按完整行稳定渲染，支持表格，减少先吐出原始 Markdown 再整体排版的突兀感。",
        "产物卡片支持预览、本地打开、在文件夹中显示、选择应用打开和复制路径。",
        "WebUI 可以登记本机项目文件夹，并按当前项目优先解析相对产物路径。",
        "长回复期间降低前端持久化、Markdown 解析和 SSE 重连重复输出的压力。",
    ],
    "fixes": [
        "修复第三次查看本机任务日志后可能无响应的问题，日志查看默认返回有界快照，只有显式 EventSource 才进入长尾流。",
        "修复 WebUI 本地文件链接和项目文件夹打开在 Windows/macOS 上不稳定的问题。",
        "修复 WebUI 打开可执行脚本文件的安全边界，默认拒绝直接启动危险扩展名，保留在文件夹中显示。",
        "修复 SSE 断线重连可能重复 delta 的问题，并为无人订阅的完成请求增加 TTL 清理。",
        "修复打包顺序导致 WebUI 静态资源可能落后一版的问题，macOS/Windows runtime staging 都在 build 后同步静态资源。",
    ],
    "howTo": [
        "需要打开产物时，在产物卡片的打开方式菜单里选择本地打开、在文件夹中显示或选择应用打开。",
        "WebUI 添加项目时输入本机绝对路径，EcoreX 会登记为当前项目工作区并创建项目记忆目录。",
        "如果任务中断或刷新页面，EcoreX 会尽量按 request id 和 SSE cursor 恢复流式输出，避免重复内容。",
    ],
    "updatePolicy": {
        "windows": "Windows 自动检测并下载更新，不强制安装；安装前会保存 UI 状态并阻止运行中任务升级。",
        "macos": "macOS 只提示新版本并跳转下载页，不自动替换本地 App 或用户数据目录。",
        "webui": "WebUI 只提示新版本并跳转下载页；发布包会校验静态资源、桥接接口和本地文件打开能力是否完整。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

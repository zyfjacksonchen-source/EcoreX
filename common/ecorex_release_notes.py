"""User-facing release notes exposed to desktop and WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.1.13",
    "title": "EcoreX 0.1.13 更新说明",
    "summary": "这次更新让多会话输入更稳，也把常用查找和 Skill 创建能力直接准备好。",
    "highlights": [
        "切换会话、新建会话或同时处理多个会话时，输入框会更快恢复到可输入状态。",
        "聊天框支持 Ctrl + Enter 换行；在 macOS 上也支持 Command + Enter 换行。",
        "内置 find skill，可以直接让 EcoreX 帮你找文件、目录或项目里的线索。",
        "内置 Skill Creator，可以直接让 EcoreX 帮你整理和创建可复用的 Skill。",
        "预置 EcoreX 自身的常用管理入口，查看 Skill、知识库和运行状态更方便。",
    ],
    "fixes": [
        "修复部分情况下切换会话后光标还在但无法输入的问题。",
        "修复新建会话后旧会话刷新回来覆盖当前输入状态的问题。",
        "减少并发会话刷新时输入框高度和焦点不同步的情况。",
    ],
    "howTo": [
        "想在同一条消息里换行，按 Ctrl + Enter；Mac 用户也可以按 Command + Enter。",
        "想找项目里的文件或目录，直接说“帮我找配置文件”或“查一下哪里有 README”。",
        "想做新的 Skill，直接说“帮我创建一个 Skill”，EcoreX 会按步骤整理。",
    ],
    "updatePolicy": {
        "windows": "Windows 版本带签名，后续可以收到一键更新提示。",
        "macos": "macOS 用户请按提示前往下载页获取最新版安装包。",
        "webui": "WebUI 更新后重新打开时，会自动显示这份更新说明。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

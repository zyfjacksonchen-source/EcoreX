"""User-facing release notes exposed to desktop and WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.1.14",
    "title": "EcoreX 0.1.14 更新说明",
    "summary": "这次更新聚焦升级、Subagent、能力安装和 WebUI 体验稳定性。",
    "highlights": [
        "Windows 桌面端支持检测并下载新版本，安装前会确认没有正在运行的任务。",
        "macOS 和 WebUI 会提示新版本并跳转下载页，下载页会按访问设备推荐安装包。",
        "新增 Subagent v1，可显式启动、查看、收集和取消子代理任务。",
        "Skill、MCP 和能力包安装改为交给 agent 处理，失败时由 agent 诊断、修复和重试。",
        "完成态消息会保留结论和产物摘要，并折叠之前的调用过程。",
    ],
    "fixes": [
        "修复任务锁残留导致会话一直停在思考中、不回复也不行动的问题。",
        "修复内部 tool-chain/system guidance 误显示在聊天或工具详情里的问题。",
        "修复 WebUI 打开项目文件夹、知识库链接和本地产物链接的路径处理。",
        "修复图片产物只显示路径的问题，现在会在聊天内直接预览。",
        "发布包增加历史私有路径和旧品牌字样扫描，避免 WebUI 安装包泄露本机源码路径。",
    ],
    "howTo": [
        "Windows 下载完成后点击安装更新；如果当前有任务运行，请等任务结束后再安装。",
        "macOS 或 WebUI 收到更新提示后点击下载页，页面会自动推荐当前设备适用的版本。",
        "需要能力包时直接点击安装，当前会话里的 agent 会负责安装、诊断和总结结果。",
    ],
    "updatePolicy": {
        "windows": "Windows 自动检测并下载更新，不强制安装；安装前会保存 UI 状态并阻止运行中任务升级。",
        "macos": "macOS 只提示新版本并跳转下载页，不自动替换本地 App 或用户数据目录。",
        "webui": "WebUI 只提示新版本并跳转下载页；发布包会扫描并阻断本机路径或旧品牌字样泄露。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

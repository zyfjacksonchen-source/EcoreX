"""User-facing release notes exposed to desktop and WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.1.18",
    "title": "EcoreX 0.1.18 更新说明",
    "summary": (
        "本次更新把桌面端和 WebUI 的 Agent 运行链路推进到生产级稳定性："
        "会话持久化、运行状态识别、SSE 恢复、取消并发、Run Center、"
        "模型调用治理和图像生成重试都完成了闭环。"
    ),
    "highlights": [
        "新增 Run Center 一级控制面，集中呈现运行中、可取消、可恢复和失败的任务状态。",
        "强化请求级运行账本和终态记录，减少刷新、重连或后台任务结束后的状态丢失。",
        "SSE 流式输出增加终态、重放缺口和 request-scoped 历史恢复能力。",
        "模型调用增加 provider capability matrix、模型调用遥测和显式失败/重试策略。",
        "图像生成与 legacy 模型路径统一 Retry-After、限流、超时和不可重试错误处理。",
    ],
    "fixes": [
        "修复部分模型或图像 provider 在 4xx/协议错误后继续错误 fallback 的问题。",
        "修复 Azure DALL-E 轮询过紧、配置 fallback 不完整和 legacy max retry 兼容问题。",
        "修复高并发取消、忙碌 fallback 和子 agent 场景下运行状态不一致的问题。",
        "修复上下文预算和工具 schema 预算在长会话中缺少显式保护的问题。",
        "修复发布包中 release notes、client key 和 WebUI version 容易滞后一版的问题。",
    ],
    "howTo": [
        "需要查看或处理运行中的任务时，进入 Run Center 查看状态、取消、恢复或重试。",
        "模型不可用、限流或超时时，界面会保留更明确的失败原因，便于切换 provider 或重试。",
        "刷新页面或恢复会话后，EcoreX 会尽量按 request id 和 SSE cursor 恢复流式输出。",
    ],
    "updatePolicy": {
        "windows": "Windows 自动检测并下载更新，不强制安装；安装前会保留 UI 状态并阻止运行中任务升级。",
        "macos": "macOS 只提示新版本并跳转下载页，不自动替换本地 App 或用户数据目录。",
        "webui": "WebUI 只提示新版本并跳转下载页；发布包会校验静态资源、桥接接口和本地文件能力。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

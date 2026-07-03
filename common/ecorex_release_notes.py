"""User-facing release notes exposed to WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.2.7",
    "revision": "2026-07-02-v027-gate-r1",
    "title": "EcoreX 0.2.7 模型切换、开箱能力与真实数据校验说明",
    "summary": (
        "本次发布聚焦真实使用链路：自定义 Gemini 可切换、同一会话切模型不失忆、"
        "Vision/OCR 与浏览器 CDP 默认可用、芯助手 CLI 按 MPI 做准确性对照，并降低大量本地文件历史带来的卡顿。"
    ),
    "highlights": [
        "自定义 Gemini 按 OpenAI-compatible custom provider 路由，`gemini-*` 不再误切到官方 Google Gemini REST。",
        "同一会话切换模型只刷新聊天模型路由，不清空 AgentBridge 的 agents/messages 缓存。",
        "进程级重建时会把用户附件和 assistant 产物的标题、类型、路径恢复为模型可见历史引用，方便继续飞书图文、轮播图等任务。",
        "模型切换提示改为分页分隔线样式，保持 `contextExcluded=true`，不会占用普通消息卡片、复制按钮或恢复入口。",
        "浏览器工具默认 CDP-first、可自动拉起受信 localhost DevTools，并在失败时安全 fallback。",
        "Vision/OCR 工具默认可发现可调用，本地 OCR 支持 RapidOCR/Pillow/Tesseract fallback。",
        "芯助手 CLI 项目/子账户枚举只从芯助手数据卷读取，MPI 作为准确性对照第一事实源。",
        "MPI 对照结果只公开 hash、计数、漂移区间和阈值，不泄露原始项目、账户、路径或指标值。",
        "大量本地文件历史会被限量、去重、截断后恢复到模型上下文；前端 token 估算也限制扫描文件、步骤和工具调用数量，降低长会话卡顿。",
        "发布与测试工件按 S0-S10 切片记录，必须经过 runtime、frontend/API、toolchain、QA/release、privacy/data 五角色 review gate。"
    ],
    "fixes": [
        "修复切换自定义 Gemini 时显示 agent error 的路由问题。",
        "修复切换模型后 `_set_chat()` 重置 bot 导致会话上下文和工具/产物链路丢失的问题。",
        "修复切换模型提示像普通消息一样长期占位、干扰后续聊天内容的问题。",
        "修复进程重启或 Agent 重建后 assistant 产物路径没有恢复到模型上下文的问题。",
        "修复 CDP auto-launch fallback 时可能先切换 launch mode、再清理 CDP 进程的清理顺序问题。",
        "修复 WebFetch 在只读 broker mock 下可能因缺少 `is_read_only()` 而不兼容的问题。",
        "修复芯助手 `project list` 缺少 `--account-id`、`--start-date`、`--end-date` 只读 flag 的问题。",
        "修复芯助手 chengfeng 分支空结果路径引用未定义 `permission_errors` 的问题。",
        "修复 MPI 不可达、样本为 0 或 fallback 冒充 MPI 时未 fail-closed 的准确性风险。",
        "修复长历史中大量附件/产物引用和工具步骤造成 token 估算与上下文恢复过重的问题。"
    ],
    "howTo": [
        "重新打开 WebUI 后，左上角应显示 v0.2.7；首次进入会弹出本更新说明。",
        "自定义 Gemini 仍在自定义模型配置中维护 key/base/model；模型列表中会标识为自定义 Gemini，不会走官方 Google Gemini provider。",
        "同一会话内可从自定义 Gemini 切回 GPT-5.5 继续任务，历史附件和 assistant 产物引用会尽量保留给新模型。",
        "切换模型后会出现一条分页分隔线，后续聊天会自然把它顶上去；它不会进入模型上下文。",
        "需要浏览器自动化时保持默认配置即可优先使用 CDP；CDP 不可用时会自动 fallback。",
        "需要 OCR/Vision 时可直接上传图片或调用工具，runtime 会按本地依赖可用性选择 RapidOCR/Pillow/Tesseract fallback。",
        "芯助手准确性测试必须以 MPI 为第一事实源，以芯助手数据卷为项目/子账户来源；MPI 不可达或样本为 0 时应阻断发布。",
        "如果长会话仍出现卡顿，请优先检查单条消息是否携带异常大的工具结果或非常多前端附件预览。"
    ],
    "updatePolicy": {
        "windows": "v0.2.7 继续以 WebUI 本地包交付；Windows 使用 manifest 校验包安装和更新。",
        "macos": "v0.2.7 继续以 WebUI 本地包交付；macOS 使用 manifest 校验包安装和更新。",
        "webui": "WebUI 通过 manifest 校验下载包、Web 服务包和静态资源；后台更新只在空闲时安装，不强制拉起新浏览器，健康检查通过后由已有页面提示刷新生效。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

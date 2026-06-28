"""User-facing release notes exposed to WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.2.4",
    "title": "EcoreX 0.2.4 WebUI 双端更新说明",
    "summary": (
        "本次发布围绕 Codex 原厂能力包兼容、Office/PDF 质量门禁、"
        "ImageGen 结构质量检查、飞书 SDK 恢复、通芯助手只读接入、"
        "会话列表视觉清理和 CowAgent 式流式 Markdown 渲染完成 WebUI 双端升级。"
    ),
    "highlights": [
        "Skill 统一按外部、自建、内置分层展示，内置原厂能力默认启用且不可关闭。",
        "PPT、Excel、Word、PDF 接入 EcoreX-native 官方能力 facade，并补齐结构、渲染和质量证据。",
        "ImageGen 增加断层、乱码、多层叠加、水印、主体结构和参考图一致性质量检查。",
        "飞书外部连接恢复 lark-oapi 运行时可用性检测和安装包依赖，避免授权后误报缺包。",
        "通芯助手 CLI 作为默认只读能力接入，所有账户数据只读权限通过 EcoreX 权限层托管。",
        "会话列表移除通用会话机器人图标和项目会话文件夹图标，保留未读橙点与运行中旋转态。",
        "长文本输出切换为 CowAgent 式边输出边排版 Markdown，降低最终排版突变。",
    ],
    "fixes": [
        "修复 Office/PDF 证据投影可能混入原始路径、正文或渲染证明的问题。",
        "修复 ImageGen 重试/finalization 证据中路径和 provider 原始 payload 泄漏风险。",
        "修复飞书注册与日志中应用 ID、密钥、文件路径和 API 原始响应的暴露风险。",
        "修复 WebUI 长文本先纯 Markdown 后整体排版导致阅读过程不连贯的问题。",
        "修复技能治理页面内置能力可被关闭、不同来源 skill 展示口径不一致的问题。",
        "修复 ImageGen provider env overlay 并发时可能串用另一请求密钥的风险。",
    ],
    "howTo": [
        "从浏览器访问 WebUI 后，先选择通用会话或项目文件夹，再直接输入需求。",
        "设置里的能力与外部连接页面可以查看内置、外部、自建能力的启用与运行时状态。",
        "生成图片、PPT、Excel、Word 或 PDF 时，EcoreX 会同步输出质量证据和失败门禁。",
        "飞书应用授权完成后，WebUI 会显示凭据、SDK 依赖和 CLI/agent readiness 的分层状态。",
    ],
    "updatePolicy": {
        "windows": "v0.2.4 继续以 WebUI 本地包交付；Windows 使用 manifest 校验包安装和更新。",
        "macos": "v0.2.4 继续以 WebUI 本地包交付；macOS 使用 manifest 校验包安装和更新。",
        "webui": "WebUI 通过 manifest 校验下载包、Web 服务包和静态资源；服务端部署后 /api/version 应返回 0.2.4。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

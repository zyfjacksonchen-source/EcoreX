"""User-facing release notes exposed to WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.3.0",
    "revision": "2026-07-07-v030-webui-active-turn-update",
    "title": "EcoreX 0.3.0 生产级任务控制与在线更新稳定性版本",
    "summary": (
        "本次版本聚焦 WebUI 生产稳定性：同会话运行中插入新消息默认更新当前任务，"
        "队列降级为显式选项；同时补齐 CDP 恢复、imagegen 产物排序、输入稳定性和在线更新状态链路。"
    ),
    "highlights": [
        "运行中再次发送默认成为“更新任务”，旧请求会被替换或合并，不再把普通发送默认塞进队列。",
        "队列、新开分支成为明确的二级选择，用户能看见系统是替换、排队还是分支执行。",
        "CDP/browser 调用遇到断连结果会自动重连重试，取消任务后不会污染下一次浏览器调用。",
        "imagegen 支持一次两图和批量多任务的稳定排序，bash 只作为确定性后处理路径。",
        "长文本输入、暂停和停止任务时保持输入框高度与滚动位置稳定。",
        "在线更新补齐签名、灰度、kill-switch、rollback 和状态机可视化。"
    ],
    "fixes": [
        "修复同会话新消息只能排队导致用户必须等待上一任务结束的问题。",
        "修复 queued guidance 主按钮语义含混、无法表达替换/排队/分支意图的问题。",
        "修复 browser 工具返回断连错误但服务未进入重连路径的问题。",
        "修复 imagegen 多图和批量任务产物命名、排序不稳定的问题。",
        "修复输入和停止任务时页面跳动、滚动被强制拉到底的问题。"
    ],
    "howTo": [
        "重新打开 WebUI 后，左上角应显示 v0.3.0；首次进入会弹出本更新说明。",
        "运行中直接输入新消息并发送，会默认更新当前任务；需要保留旧任务时使用“排队稍后执行”。",
        "需要从当前上下文分出独立路线时，选择“新开分支”。",
        "图片生成由 imagegen 工具层决定，bash 脚本只用于重命名、压缩、合并等确定性后处理。",
        "在线更新会等待当前请求空闲，下载、校验、暂存、延迟安装和回滚状态会在 WebUI 中显示。"
    ],
    "updatePolicy": {
        "windows": "v0.3.0 继续以 WebUI 本地包交付；Windows 使用 manifest 校验包安装和更新。",
        "macos": "v0.3.0 继续以 WebUI 本地包交付；macOS 使用 manifest 校验包安装和更新。",
        "webui": "WebUI 通过 manifest、release-index 和签名元数据校验下载包、Web 服务包和静态资源；后台更新只在空闲时安装，健康检查通过后由已有页面切换，失败自动回滚。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

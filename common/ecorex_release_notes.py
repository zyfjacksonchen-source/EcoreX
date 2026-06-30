"""User-facing release notes exposed to WebUI clients."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CURRENT_RELEASE_NOTES: Dict[str, Any] = {
    "version": "0.2.5",
    "title": "EcoreX 0.2.5 运行时与工具矩阵更新说明",
    "summary": (
        "本次发布把 WebUI 本地包和 Linux Web 服务收束到 EcoreX 自有运行时、"
        "依赖与工具执行边界，并补齐 Web 端权限、飞书、通芯助手 CLI、会话历史恢复和生图路由体验。"
    ),
    "highlights": [
        "新增 RuntimeDependencyProvider，默认只接受 EcoreX 自有 runtime/state 中的 Python、Node、CLI 和包路径。",
        "本地工具统一通过 ToolExecutionEnvironment 执行，集中处理 PATH、NODE_PATH、PYTHONPATH、工作目录、超时和脱敏。",
        "Windows、macOS 与 Linux service 发布包写入 v0.2.5 runtime-manifest，并由发布校验器检查相对路径、依赖来源和包一致性。",
        "Skill、能力包、扩展和工具视图共享 toolBinding 合约，ImageGen、Office/PDF、Browser、MCP、飞书和通芯助手都有统一可探测状态。",
        "工具矩阵覆盖 current、clean-path、clean-user-state 三种本地环境，生产 service-user 探针单独保留为上线后严格门禁。",
        "通芯助手 CLI 支持配置 EcoreX 自有 Python，并通过 runpy shim 运行外部脚本，保持只读能力和本地模块隔离。",
        "飞书 CLI 授权改为会话化续接：WebUI 先展示官方授权链接/二维码，用户授权后由 agent_auth_status 使用内部保存的 device code 完成写回。",
        "WebUI 权限只展示默认权限和完全访问权限；完全访问权限会放开系统 PATH、Node/npx、Python 与常用 shell 工具环境。",
        "通芯助手 CLI 默认走服务器远端 auth/bootstrap 配置，用户输入账号密码即可拉取受控的 xin_agent_cli.py；本地脚本仅作为显式兜底。",
        "通芯助手远端 bootstrap 支持多文件包，可随 `xin_agent_cli.py` 一起安装 `models.py` 或 `models/__init__.py` 等只读依赖模块。",
        "WebUI 侧边栏将通用会话拆成置顶任务和任务两组，置顶任务按最近置顶时间排在组内最上方；项目会话置顶仍保留在项目内。",
        "WebUI 启动时会先把浏览器本地保留的旧会话正文导入后端历史库，后续用户消息和最终回复也会双保险落库。",
        "图片生成、改图和参考图生成统一走原生 imagegen 路由，默认从 `gpt-image-2-pro` 开始；`image_urls` 会被归一到 GPT Image edits/reference 路由。",
        "PPT、文档、图片、表格等交付型任务的最终回复收敛为结果、路径/链接、验证状态和必要下一步，避免冗长日志和出厂设置追问。",
        "发布页、安装脚本、WebUI 左上角版本和更新说明统一到 v0.2.5。"
    ],
    "fixes": [
        "修复真实发布包中 /api/version 仍返回旧版号、WebUI 左上角显示旧版本的问题。",
        "修复更新说明仍停留在 v0.2.4 文案的问题。",
        "修复通芯助手已配置脚本路径但因缺少 EcoreX 自有 Python 而显示 dependency_missing 的问题。",
        "修复外部通芯助手脚本在 ambient PYTHONPATH 下可能被 EcoreX config.py/models.py 影子模块干扰的问题。",
        "修复通芯助手缺少认证/登录地址的问题；WebUI 安装脚本写入默认通芯认证入口，便于其他用户 bootstrap xin_agent_cli.py。",
        "修复通芯助手远端配置时仍要求用户提供 bootstrap_url 和 SHA256 的问题；有远端 auth 时优先提示账号密码认证。",
        "修复远端下载的芯助手 CLI 通过 SHA256 校验后，因 bootstrap 包缺少真实 `models.database` / `models.DATABASE` 导出而只能提示泛化依赖失败的问题。",
        "修复远端 bootstrap 只能安装单个 `xin_agent_cli.py`、无法携带 `models` 数据层依赖的问题；注释里出现 DATABASE 不再被误判为有效导出。",
        "修复用户选择完全访问权限后 npx、node、python 或基础 shell 仍被 EcoreX 运行时误拦截的问题。",
        "修复旧的 read-only/always-ask/custom 权限状态在 WebUI 中造成“默认权限”显示但实际拦截的迁移问题。",
        "修复飞书 CLI 点击授权后 WebUI 丢失后续写回状态的问题，避免授权链接跳转后无返回提示。",
        "修复 Web 流式会话中可弹权限请求却被后端判定为非交互环境、直接默认拒绝的问题。",
        "修复用户难以区分置顶会话和普通会话的问题，并补齐置顶时间持久化。",
        "修复升级后旧会话正文只存在浏览器缓存、未导入后端历史库，导致打开会话像“丢记录”的问题。",
        "修复改图/参考图生成传入 `image_urls` 时热路径丢失参考图，可能退化成纯文本生图或诱导脚本式生成的问题。",
        "修复交付型任务完成后仍追加命名/交流风格等出厂设置问题的问题。",
        "修复 schema-only 或 discovery-only 状态可能把飞书/通芯助手能力误提升为 ready 的问题。",
        "修复发布证据中可能混入未脱敏路径、CLI 原始输出或 Codex 私有 runtime 线索的风险。"
    ],
    "howTo": [
        "重新打开 WebUI 后，左上角应显示 v0.2.5；首次进入会弹出本更新说明。",
        "Windows 或 macOS 用户继续运行下载页上的同一条一键命令，脚本会读取 manifest、校验 SHA256 并替换本地 WebUI 包。",
        "设置里的能力与外部连接页面可以查看内置、外部、自建能力的启用、依赖和工具绑定状态。",
        "权限设置只需在默认权限和完全访问权限之间切换；需要让 Agent 自行运行 npx、node、python 或安装优先依赖时请选择完全访问权限。",
        "飞书用户授权时保持 WebUI 打开的授权卡片；点击授权页后回到 WebUI，系统会用内部 sessionId 继续轮询并完成 user 写入。",
        "配置通芯助手时优先使用远端登录；输入通芯账号密码后，WebUI 会通过服务器 auth/bootstrap 拉取 xin_agent_cli.py。本地脚本路径只作为兜底。",
        "如果通芯提供方更新远端包，请确保 manifest 的 `files` / `bootstrapFiles` 包含 `models.py` 或 `models/__init__.py`，并真实导出 `database` 或 `DATABASE`。",
        "通芯助手保持只读默认能力；写入类命令仍会在执行前被权限边界拦截。",
        "通用会话列表中，置顶任务显示在独立分区；项目会话即使置顶，也只在所属项目的会话列表内上移。",
        "升级后请重新打开或刷新 WebUI；浏览器里仍保留的旧会话正文会在启动时自动导入服务端历史库。",
        "生图、改图和参考图生成都应调用原生 imagegen；没有用户指定模型时默认走 `gpt-image-2-pro`，不要用 Python/PIL/HTML/SVG 代替真实生图。",
        "部署人员可用 v0.2.5 release gate 报告区分本地通过与生产 service-user 探针待补状态。"
    ],
    "updatePolicy": {
        "windows": "v0.2.5 继续以 WebUI 本地包交付；Windows 使用 manifest 校验包安装和更新。",
        "macos": "v0.2.5 继续以 WebUI 本地包交付；macOS 使用 manifest 校验包安装和更新。",
        "webui": "WebUI 通过 manifest 校验下载包、Web 服务包和静态资源；服务端部署后 /api/version 应返回 0.2.5，并携带本更新说明。",
    },
}


def get_current_release_notes() -> Dict[str, Any]:
    """Return a copy so request handlers cannot mutate the shared notes."""

    return deepcopy(CURRENT_RELEASE_NOTES)

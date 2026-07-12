"""
Agent Stream Execution Module - Multi-turn reasoning based on tool-call

Provides streaming output, event system, and complete tool-call loop
"""
import json
import hashlib
import os
import re
import shlex
import threading
import time
from typing import List, Dict, Any, Optional, Callable, Tuple

from agent.protocol.cancel import AgentCancelledError
from agent.protocol.models import LLMRequest, LLMModel
from agent.protocol.message_utils import sanitize_claude_messages, compress_turn_to_text_only
from agent.protocol.task_observer import TaskObserver
from agent.core.tool_router import ToolRouterPolicy
from agent.skills.tool_bridge import SKILL_CALLABLE_TOOL_ALIASES
from agent.tools.base_tool import BaseTool, ToolResult
from common.ecorex_public_payload import mask_sensitive_text, redact_public_tool_value
from common.ecorex_identity import sanitize_assistant_identity, sanitize_message_identity
from common.log import logger
from common.i18n import t as _t

# Optional: repair malformed JSON args from non-strict providers (e.g. unescaped quotes in long content).
try:
    from json_repair import repair_json as _repair_json
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False


# Maximum number of characters of model "reasoning / thinking" content to persist
# in conversation history. The full reasoning is still streamed to the UI in real
# time (subject to its own SSE / rendering limits); this bound only controls what
# is stored in DB and replayed in history. Long reasoning is not useful for later
# context (the LLM never sees thinking blocks anyway) and bloats DB.
# Keep aligned with the Web SSE reasoning cap so refresh/recovery does not
# collapse a long visible thinking trace back to a tiny historical preview.
MAX_STORED_REASONING_CHARS = 256 * 1024

# Marker inserted between head and tail when reasoning is truncated.
_REASONING_TRUNCATE_MARKER = "\n\n... [reasoning truncated, {omitted} chars omitted] ...\n\n"


def _public_agent_exception_summary(value: Any) -> Dict[str, Any]:
    text = str(value or "")
    text_bytes = text.encode("utf-8", errors="replace")
    text_hash = hashlib.sha256(text_bytes).hexdigest()[:16] if text else ""
    error_type = value.__class__.__name__ if value is not None else ""
    return {
        "errorHash": text_hash,
        "error_hash": text_hash,
        "errorType": error_type,
        "error_exception_type": error_type,
        "errorLength": len(text),
        "error_chars": len(text),
        "errorBytes": len(text_bytes),
        "error_bytes": len(text_bytes),
        "redacted": True,
        "errorRedacted": True,
    }


def _public_agent_exception_message(prefix: str, value: Any) -> str:
    summary = _public_agent_exception_summary(value)
    if not summary["errorHash"]:
        return prefix
    return (
        f"{prefix} Details redacted "
        f"(type={summary['errorType']}, hash={summary['errorHash']}, "
        f"chars={summary['errorLength']}, bytes={summary['errorBytes']})."
    )


def _private_agent_exception_text_for_classification(value: Any) -> str:
    """Use exception text only for local retry/overflow classification."""
    if value is None:
        return ""
    return "{}".format(value)


def _model_content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_model_content_to_text(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _model_content_to_text(value.get("text"))
        if "content" in value:
            return _model_content_to_text(value.get("content"))
        if "output_text" in value:
            return _model_content_to_text(value.get("output_text"))
        if "value" in value and value.get("type") in ("text", "output_text", None):
            return _model_content_to_text(value.get("value"))
        return ""
    return str(value)


def _safe_tool_arg_log_value(key: Any, value: Any, max_chars: int = 200) -> str:
    key_text = str(key or "")
    try:
        safe_container = redact_public_tool_value({key_text: value}, max_depth=4, max_items=20, max_chars=max_chars)
        safe_value = safe_container.get(key_text) if isinstance(safe_container, dict) else safe_container
    except Exception:
        safe_value = "[redacted]"
    if isinstance(safe_value, (dict, list)):
        text = json.dumps(safe_value, ensure_ascii=False)
    else:
        text = str(safe_value)
    if len(text) > max_chars:
        text = text[:max_chars] + f"...({len(text)} chars)"
    return text


def _safe_tool_result_log_preview(value: Any, max_chars: int = 200) -> str:
    try:
        safe_value = redact_public_tool_value(value, max_depth=5, max_items=20, max_chars=max(512, max_chars))
    except Exception:
        safe_value = "[redacted]"
    if isinstance(safe_value, (dict, list)):
        text = json.dumps(safe_value, ensure_ascii=False)
    else:
        text = mask_sensitive_text(safe_value, max_chars=max(512, max_chars))
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _public_agent_tool_error_result(prefix: str, value: Any, *, execution_time: float = 0) -> Dict[str, Any]:
    message = _public_agent_exception_message(prefix, value)
    return {
        "status": "error",
        "result": message,
        "error": message,
        "execution_time": execution_time,
        **_public_agent_exception_summary(value),
    }

TOOL_SCHEMA_BUDGET_ENABLED_DEFAULT = True
CONTEXT_BUDGET_WARN_RATIO_DEFAULT = 0.85
TOOL_EXECUTION_HEARTBEAT_SECONDS = 12
TOOL_EXECUTION_DEFAULT_LEASE_SECONDS = 15 * 60
TOOL_EXECUTION_DEFAULT_MAX_SECONDS = 90 * 60
TOOL_EXECUTION_LONG_TASK_MAX_SECONDS = 3 * 60 * 60
TOOL_EXECUTION_EXTENSION_SECONDS = 15 * 60
TOOL_EXECUTION_LONG_TASK_KEYWORDS = (
    "image",
    "images",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "generate",
    "render",
    "comfy",
    "diffusion",
    "stable-diffusion",
    "gpt-image",
    "dall",
    "midjourney",
    "remotion",
    "video",
    "ffmpeg",
    "playwright install",
    "browser-automation",
    "npm install",
    "pip install",
    "docker build",
    "pytest",
    "npm run build",
    "生图",
    "生成图片",
    "图片",
    "渲染",
    "视频",
)
CONTEXT_OVERFLOW_KEYWORDS = (
    "context length exceeded",
    "maximum context length",
    "prompt is too long",
    "context overflow",
    "context window",
    "exceeds model context",
    "request_too_large",
    "request too large",
    "prompt too large",
    "input too large",
    "request exceeds the maximum size",
    "tokens exceed",
)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 24 * 60 * 60) -> int:
    raw_value = os.environ.get(name, "")
    if raw_value:
        try:
            parsed = int(float(raw_value))
            return max(minimum, min(parsed, maximum))
        except Exception:
            logger.warning(
                "[Agent] invalid %s=%r; using default %ss",
                name,
                raw_value,
                default,
            )
    return max(minimum, min(default, maximum))


def _coerce_timeout_hint_seconds(value: Any) -> Optional[int]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    if parsed > 24 * 60 * 60 * 100:
        parsed = parsed / 1000.0
    return int(parsed)


def _tool_timeout_policy(tool_name: str, arguments: Any) -> Dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    args_text = ""
    try:
        args_text = json.dumps(args, ensure_ascii=False).lower()
    except Exception:
        args_text = str(args).lower()

    explicit_hint = None
    for key in ("timeout_seconds", "timeout_secs", "timeout"):
        explicit_hint = _coerce_timeout_hint_seconds(args.get(key))
        if explicit_hint:
            break
    if explicit_hint is None:
        explicit_hint = _coerce_timeout_hint_seconds(args.get("timeout_ms"))

    base_seconds = _env_int(
        "ECOREX_TOOL_EXECUTION_LEASE_SECONDS",
        TOOL_EXECUTION_DEFAULT_LEASE_SECONDS,
        minimum=60,
        maximum=24 * 60 * 60,
    )
    max_seconds = _env_int(
        "ECOREX_TOOL_EXECUTION_MAX_SECONDS",
        TOOL_EXECUTION_DEFAULT_MAX_SECONDS,
        minimum=base_seconds,
        maximum=24 * 60 * 60,
    )
    extension_seconds = _env_int(
        "ECOREX_TOOL_EXECUTION_EXTENSION_SECONDS",
        TOOL_EXECUTION_EXTENSION_SECONDS,
        minimum=60,
        maximum=24 * 60 * 60,
    )

    name = str(tool_name or "").lower()
    is_long_task = name in {"subagent", "agent_capability", "optional_abilities"} or any(
        keyword in args_text or keyword in name for keyword in TOOL_EXECUTION_LONG_TASK_KEYWORDS
    )
    reason = "default"

    if is_long_task:
        base_seconds = max(base_seconds, 30 * 60)
        max_seconds = max(max_seconds, TOOL_EXECUTION_LONG_TASK_MAX_SECONDS)
        reason = "long_running_tool"

    if explicit_hint:
        base_seconds = max(base_seconds, min(explicit_hint + 60, 24 * 60 * 60))
        max_seconds = max(max_seconds, min(explicit_hint + 30 * 60, 24 * 60 * 60))
        reason = "tool_requested_timeout"

    max_seconds = max(base_seconds, max_seconds)
    adaptive = is_long_task or explicit_hint is not None
    return {
        "lease_seconds": base_seconds,
        "max_seconds": max_seconds,
        "extension_seconds": extension_seconds,
        "adaptive": adaptive,
        "reason": reason,
    }

TOOL_SCHEMA_CORE_NAMES = {
    "read",
    "ls",
    "find",
    "bash",
    "write",
    "edit",
    "send",
    "host_diagnostics",
    "optional_abilities",
    "agent_capability",
    "feishu_cli",
    "tongxin_cli",
    "ecorex_cli",
}

TOOL_NAME_ALIASES = {
    "shell": "bash",
    "terminal": "bash",
    "cmd": "bash",
    "powershell": "bash",
    "image_generation": "imagegen",
    "image-generation": "imagegen",
    "tongxin": "tongxin_cli",
    "tongxin-cli": "tongxin_cli",
    "xin_agent_cli": "tongxin_cli",
    "xin-agent-cli": "tongxin_cli",
}
TOOL_NAME_ALIASES.update(SKILL_CALLABLE_TOOL_ALIASES)

TOOL_SCHEMA_INTENT_KEYWORDS = {
    "workspace": (
        "read", "ls", "find", "file", "files", "path", "directory", "folder",
        "附件", "文件", "路径", "目录", "文件夹", "本地", "读取文件", "找文件", "查看文件",
    ),
    "browser": (
        "browser", "chrome", "cdp", "devtools", "playwright", "http://", "https://",
        "xhslink", "xiaohongshu", "小红书", "网页", "浏览器", "打开网页", "读取链接", "链接", "点击", "截图",
    ),
    "web": (
        "web", "search", "fetch", "http://", "https://", "latest", "today", "新闻", "搜索", "联网", "查一下",
    ),
    "feishu": (
        "feishu", "lark", "飞书", "多维表格", "妙搭", "妙记", "日历", "审批", "通讯录", "bitable",
    ),
    "tongxin": (
        "tongxin", "xin_agent", "xin agent", "芯助手", "通芯", "实时消耗", "账号数据", "三端口",
        "本土小红书", "医美小红书", "乘风小红书", "mpi", "广告主", "消耗", "展现", "点击",
    ),
    "office": (
        "office", "document", "documents", "word", "doc", "docx", "pdf",
        "presentation", "presentations", "powerpoint", "ppt", "pptx", "slides",
        "spreadsheet", "spreadsheets", "excel", "workbook", "xlsx", "xlsm", "csv", "tsv",
        "文档", "word文档", "docx", "pdf", "幻灯片", "演示文稿", "ppt", "pptx",
        "表格", "电子表格", "excel", "xlsx", "工作簿", "质量检查", "渲染预览",
    ),
    "scheduler": (
        "schedule", "scheduler", "remind", "cron", "定时", "提醒", "自动化", "每天", "每周",
    ),
    "subagent": (
        "subagent", "sub-agent", "sub agent", "parallel", "并发", "子任务", "多角度", "复审",
    ),
    "vision": (
        "image", "vision", "screenshot", "图片", "图像", "截图", "识别",
    ),
    "imagegen": (
        "imagegen", "image gen", "image generation", "text to image", "image to image",
        "generate image", "edit image", "生图", "图像生成", "图片生成", "文生图", "图生图",
        "生成图片", "生成图像", "改图", "修图", "出图", "多图", "批量生图",
        "批量生成图片", "一张张生成", "逐张生成", "轮播图",
        "精准修图", "局部修图", "精修标注", "标注图", "箭头尖端", "语义图片编辑",
    ),
    "ocr": (
        "ocr", "extract text", "extract url", "screenshot link", "image link",
        "图片链接", "截图链接", "识别链接", "读取链接", "读链接", "识别文字", "识别文本",
    ),
    "memory": (
        "memory", "remember", "recall", "记忆", "回忆",
    ),
    "diagnostics": (
        "diagnostic", "diagnose", "mcp", "permission", "install", "ability", "tool missing",
        "config", "configure", "api key", "api_key", "_api_key", "apikey", "secret", "env", "environment",
        "诊断", "权限", "安装", "能力", "工具缺失", "配置", "密钥", "环境变量",
    ),
}

IMAGEGEN_SEMANTIC_EDIT_REGEXES = (
    re.compile(r"(?:图|图片|照片|图像|画面|海报|封面|素材).{0,32}(?:去掉|去除|删除|移除|抹掉|擦除|消除|去背景|换背景|去水印|去logo|去\s*logo|修掉|修复|补全|扩图|抠图|换成|替换|改成|改为|变成|调整|美化)"),
    re.compile(r"(?:去掉|去除|删除|移除|抹掉|擦除|消除|去背景|换背景|去水印|去logo|去\s*logo|修掉|修复|补全|扩图|抠图|换成|替换|改成|改为|变成|调整|美化).{0,32}(?:图|图片|照片|图像|画面|海报|封面|素材|背景|路人|水印|logo|文字|人物|物体|瑕疵|污渍)"),
    re.compile(r"(?:背景|路人|水印|logo|文字|错字|物体|人物|瑕疵|污渍).{0,20}(?:去掉|去除|删除|移除|抹掉|擦除|消除|换成|替换|改成|改为|修掉|修复)"),
    re.compile(r"(?i)(?:remove|delete|erase|replace|change|fix|retouch|inpaint|clean up).{0,40}(?:image|photo|picture|background|person|people|watermark|logo|text|object)"),
    re.compile(r"(?i)(?:image|photo|picture).{0,40}(?:remove|delete|erase|replace|change|fix|retouch|inpaint|clean up)"),
)

IMAGEGEN_INTENT_REGEXES = (
    re.compile(r"(?:生成|画|绘制|出|做|设计|创作).{0,16}(?:[一二两三四五六七八九十\d]+\s*张).{0,18}(?:图|图片|图像|海报|插图|插画|封面|视觉|素材)?"),
    re.compile(r"(?:生成|画|绘制|出|做|设计|创作).{0,10}(?:图|图片|图像|海报|插图|插画|封面|视觉|素材)"),
    re.compile(r"[一二两三四五六七八九十\d]+\s*张.{0,12}(?:图|图片|图像|海报|插图|插画|封面|视觉|素材)"),
    re.compile(r"(?:一张张|逐张|多图|批量).{0,12}(?:生成|生图|出图|画|做|设计|创作)"),
    re.compile(r"(?:精准修图|局部修图|精修标注|语义图片编辑|标注图|箭头尖端)"),
    re.compile(r"(?:单字|一个字|文字|错字).{0,20}(?:改图|修图|改成|改为|替换|换成)"),
    re.compile(r"(?:海报|图片|图像|画面|封面|物料).{0,24}(?:改图|修图|重绘|生成|出图|改成|改为|替换|换成)"),
    *IMAGEGEN_SEMANTIC_EDIT_REGEXES,
)
IMAGEGEN_PRIORITY_TOOL_NAMES = {
    "imagegen",
    "host_diagnostics",
    "optional_abilities",
    "agent_capability",
    "ecorex_cli",
}
IMAGEGEN_COMPANION_INTENT_GROUPS = {
    "browser",
    "web",
    "ocr",
    "workspace",
}
IMAGEGEN_SHELL_SEMANTIC_SIGNAL_REGEXES = (
    re.compile(r"(?i)(?:^|[\s;,{(\[])(?:prompt|instruction|description)\s*[:=]"),
    re.compile(r"(?i)--prompt(?:=|\s+)"),
    re.compile(r"(?i)\"prompt\"\s*:"),
    re.compile(r"(?:生成|生图)"),
) + IMAGEGEN_SEMANTIC_EDIT_REGEXES

TOOL_SCHEMA_FOLLOWUP_CONFIRMATIONS = {
    "ok", "okay", "yes", "y", "go", "continue", "proceed", "do it", "run it", "execute",
    "好的", "好", "可以", "继续", "执行", "开始", "确认", "嗯", "行",
    "已登录", "已经登录", "登录完成", "我已登录", "我已经登录",
    "已扫码", "扫码完成", "已授权", "授权完成",
}


def _is_real_user_query_message(message: dict, expected_text: str = "") -> bool:
    """True for the user's prompt, false for role=user tool_result messages."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content", [])
    if isinstance(content, str):
        return not expected_text or content == expected_text
    if not isinstance(content, list):
        return False
    has_tool_result = any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )
    if has_tool_result:
        return False
    text_blocks = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if not text_blocks:
        return False
    return not expected_text or "\n".join(text_blocks) == expected_text


def new_messages_since_user_query(messages: list, original_length: int, user_message: str = "") -> list:
    """Return the current user query plus all messages produced by this run.

    Context trimming can make the final message list shorter than the original
    list, but a long tool chain can grow it back past ``original_length``. A
    length-only test then persists only the tail, so locate the run boundary by
    the real user query instead.
    """
    if not isinstance(messages, list):
        return []
    fallback_start = min(max(int(original_length or 0), 0), len(messages))
    for idx in range(len(messages) - 1, -1, -1):
        if _is_real_user_query_message(messages[idx], user_message):
            return list(messages[idx:])
    for idx in range(len(messages) - 1, -1, -1):
        if _is_real_user_query_message(messages[idx]):
            return list(messages[idx:])
    return list(messages[fallback_start:])


def _coerce_usage_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_usage(usage: dict, model_name: str = "") -> dict:
    """Normalize OpenAI-compatible usage envelopes for desktop telemetry."""
    if not isinstance(usage, dict):
        return {}
    input_tokens = _coerce_usage_int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("promptTokens")
    )
    output_tokens = _coerce_usage_int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("completionTokens")
        or usage.get("outputTokens")
    )
    total_tokens = _coerce_usage_int(
        usage.get("total_tokens")
        or usage.get("totalTokens")
    )
    if total_tokens <= 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    if total_tokens <= 0:
        return {}
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "model": model_name or "",
    }


def _user_visible_llm_error(message: Any, status_code: Any = "", error_code: Any = "", error_type: Any = "") -> str:
    raw = str(message or "").strip()
    raw_lower = raw.lower()
    status_text = str(status_code or "").strip()
    if (
        status_text in ("0", "N/A")
        or "connectionreseterror" in raw_lower
        or "connection reset by peer" in raw_lower
        or "connection aborted" in raw_lower
        or raw_lower.startswith("connection error")
        or raw_lower.startswith("stream interrupted")
        or raw_lower.startswith("stream error")
    ):
        return _t(
            "网络连接被中断，请稍后重试；如果持续出现，请检查当前网络、代理或模型接口地址。",
            "The network connection was interrupted. Please try again later; if it keeps happening, check the network, proxy, or model endpoint.",
        )
    if "timed out" in raw_lower or "timeout" in raw_lower:
        return _t(
            "模型接口响应超时，请稍后重试；如果持续出现，请检查当前网络、代理或模型接口地址。",
            "The model endpoint timed out. Please try again later; if it keeps happening, check the network, proxy, or model endpoint.",
        )
    if status_text and status_text not in ("", "N/A"):
        code_text = str(error_code or "").strip()
        type_text = str(error_type or "").strip()
        extras = []
        if code_text:
            extras.append(f"Code: {code_text}")
        if type_text:
            extras.append(f"Type: {type_text}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"{raw} (Status: {status_text}){suffix}"
    return re.sub(r"\s+\(Status:\s*0,\s*Code:\s*,\s*Type:\s*\)\s*$", "", raw).strip() or _t(
        "模型接口请求失败，请稍后重试。",
        "The model request failed. Please try again later.",
    )


def _truncate_reasoning_for_storage(text: str) -> str:
    """Trim long reasoning to head + tail with an omission marker.

    Keeps the first and last halves of MAX_STORED_REASONING_CHARS so both the
    initial chain-of-thought and the final conclusions are preserved for UI
    replay, without storing the entire (often very large) middle.
    """
    if not text:
        return text
    if len(text) <= MAX_STORED_REASONING_CHARS:
        return text
    half = MAX_STORED_REASONING_CHARS // 2
    head = text[:half]
    tail = text[-half:]
    omitted = len(text) - len(head) - len(tail)
    return head + _REASONING_TRUNCATE_MARKER.format(omitted=omitted) + tail


def _parse_tool_args(args_str: str, finish_reason: Optional[str]) -> Tuple[dict, Optional[str]]:
    """Parse tool args JSON. Returns (args, error_msg); error_msg is None on success.

    On JSONDecodeError: detect truncation first (skip repair, surface max_tokens hint);
    otherwise try json-repair for escape issues; finally fall back to the raw decoder error.
    """
    if not args_str:
        return {}, None
    try:
        return json.loads(args_str), None
    except json.JSONDecodeError as e:
        if finish_reason in ("length", "max_tokens") or not args_str.rstrip().endswith("}"):
            return {}, "Output truncated (max_tokens reached). Split content into smaller chunks across multiple tool calls."
        if _HAS_JSON_REPAIR:
            try:
                repaired = _repair_json(args_str, return_objects=True)
                if isinstance(repaired, dict):
                    logger.warning(f"Tool args JSON repaired ({len(args_str)} chars)")
                    return repaired, None
            except Exception:
                pass
        return {}, f"Invalid JSON in tool arguments: {e.msg}"


class AgentStreamExecutor:
    """
    Agent Stream Executor
    
    Handles multi-turn reasoning loop based on tool-call:
    1. LLM generates response (may include tool calls)
    2. Execute tools
    3. Return results to LLM
    4. Repeat until no more tool calls
    """

    def __init__(
            self,
            agent,  # Agent instance
            model: LLMModel,
            system_prompt: str,
            tools: List[BaseTool],
            max_turns: int = 50,
            on_event: Optional[Callable] = None,
            messages: Optional[List[Dict]] = None,
            max_context_turns: int = 30,
            cancel_event=None,
    ):
        """
        Initialize stream executor
        
        Args:
            agent: Agent instance (for accessing context)
            model: LLM model
            system_prompt: System prompt
            tools: List of available tools
            max_turns: Maximum number of turns
            on_event: Event callback function
            messages: Optional existing message history (for persistent conversations)
            max_context_turns: Maximum number of conversation turns to keep in context
            cancel_event: Optional threading.Event used to signal user cancel.
                Checked at every safe point (turn boundary, before tool execution,
                during LLM streaming). When set, raises AgentCancelledError which
                run_stream catches to gracefully wind down.
        """
        self.agent = agent
        self.model = model
        self.system_prompt = system_prompt
        # Convert tools list to dict
        self.tools = {tool.name: tool for tool in tools} if isinstance(tools, list) else tools
        self.max_turns = max_turns
        self.on_event = on_event
        self.max_context_turns = max_context_turns
        self.cancel_event = cancel_event

        # Message history - use provided messages or create new list
        self.messages = messages if messages is not None else []
        
        # Tool failure tracking for retry protection
        self.tool_failure_history = []  # List of (tool_name, args_hash, success) tuples
        # Tool-chain tracking catches loops where the model changes arguments but
        # keeps probing the same external system (for example bash -> lark-cli).
        self.tool_chain_history = []  # List of (chain_key, tool_name, success) tuples
        self._last_convergence_hint_key = ""
        self._force_text_response_next_turn = False
        self._force_text_response_reason = ""
        self._internal_hint_texts = []
        self._last_model_retry_evidence: Dict[str, Any] = {}
        self._current_user_message_text = ""
        self._current_turn_imagegen_success = False
        
        # Track files to send (populated by read tool)
        self.files_to_send = []  # List of file metadata dicts

    def _check_cancelled(self) -> None:
        """Raise AgentCancelledError if the user requested cancellation.

        Called at safe points (turn start, between tool calls, between LLM
        chunks). Cheap to call: just an Event.is_set() probe.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentCancelledError("agent cancelled by user")

    def _handle_cancelled(self, partial_response: str) -> None:
        """Wind down ``self.messages`` after a user-initiated cancel.

        The messages list may be in any of these states when we get here:
          (a) Last message is an assistant message containing tool_use
              blocks but the matching tool_result has not been appended yet.
          (b) Last message is an assistant text-only reply (cancel happened
              right before the next turn started).
          (c) Last message is a user tool_result message and we cancelled
              between turns.

        For (a) we MUST synthesise tool_result blocks, otherwise the next
        request will fail Claude/OpenAI's strict pairing validation. For
        (b)/(c) the state is already valid and we just append a small
        cancellation note so the user/LLM both see the boundary clearly.
        """
        try:
            # Step 1: close any orphaned tool_use in the trailing assistant
            # message by injecting matching tool_result blocks.
            if self.messages and isinstance(self.messages[-1], dict) \
                    and self.messages[-1].get("role") == "assistant":
                last = self.messages[-1]
                content = last.get("content")
                if isinstance(content, list):
                    pending_tool_use_ids = [
                        block.get("id")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    ]
                    pending_tool_use_ids = [tid for tid in pending_tool_use_ids if tid]
                    if pending_tool_use_ids:
                        tool_result_blocks = [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": "Cancelled by user before this tool finished.",
                                "is_error": True,
                            }
                            for tid in pending_tool_use_ids
                        ]
                        self.messages.append({
                            "role": "user",
                            "content": tool_result_blocks,
                        })
                        logger.info(
                            f"[Agent] Injected {len(tool_result_blocks)} cancellation "
                            f"tool_result blocks to keep message history valid"
                        )

            # Step 2: append a stable "interrupted" marker so the LLM sees a
            # clear stop boundary on the next turn.
            self.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": "_(Cancelled by user)_"}],
            })
        except Exception as e:
            logger.warning(f"[Agent] _handle_cancelled cleanup failed: {_public_agent_exception_message('Cleanup failed.', e)}")

    def _emit_event(self, event_type: str, data: dict = None):
        """Emit event"""
        if self.on_event:
            try:
                self.on_event({
                    "type": event_type,
                    "timestamp": time.time(),
                    "data": data or {}
                })
            except Exception as e:
                logger.error(f"Event callback error: {_public_agent_exception_message('Event callback failed.', e)}")

    def _image_artifact_naming_context(self) -> Dict[str, Any]:
        """Build a small, user-visible naming context for generated image files."""
        session_id = str(getattr(self.agent, "_current_session_id", "") or "").strip()
        request_id = str(getattr(self.agent, "_current_request_id", "") or "").strip()
        summary = ""
        if session_id:
            try:
                from agent.memory.conversation_store import get_conversation_store

                store = get_conversation_store()
                title_state = store.get_session_title_state(session_id)
                if isinstance(title_state, dict):
                    summary = str(title_state.get("title") or "").strip()
                if not summary:
                    latest_user = store.get_visible_user_message(session_id)
                    summary = str(latest_user.get("text") or "").strip()
            except Exception as exc:
                logger.debug("[Agent] image artifact naming context unavailable: %s", exc.__class__.__name__)
        return {
            "sessionId": session_id,
            "requestId": request_id,
            "summary": summary or "图片产物",
            "source": "conversation-summary",
        }

    def _authorize_tool_execution(self, tool_name: str, tool_id: str, arguments: dict) -> dict:
        """Ask the desktop permission broker before high-risk local tools run."""
        def normalize_decision(decision: Any) -> dict:
            if isinstance(decision, dict) and decision.get("allowed") in {True, False}:
                return decision
            return {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}

        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            action = ""
            if isinstance(arguments, dict):
                action = str(arguments.get("action") or "")
            normalized_tool = str(tool_name or "").strip().lower()
            if normalized_tool in {"bash", "shell", "terminal"}:
                action = "system_shell"
            broker = get_tool_permission_broker()
            capability_authorize = getattr(broker, "authorize_capability", None)
            if callable(capability_authorize):
                decision = capability_authorize(
                    capability=tool_name,
                    action=action,
                    tool_call_id=tool_id,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    emit_event=self._emit_event,
                    cancel_event=self.cancel_event,
                )
                return normalize_decision(decision)
            legacy_authorize = getattr(broker, "authorize", None)
            if callable(legacy_authorize):
                decision = legacy_authorize(
                    tool_name=tool_name,
                    tool_call_id=tool_id,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    emit_event=self._emit_event,
                    cancel_event=self.cancel_event,
                )
                if isinstance(decision, dict):
                    return normalize_decision(decision)
            return {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}
        except AgentCancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Agent] desktop tool permission check skipped: {_public_agent_exception_message('Permission check failed.', e)}")
            risky = (tool_name or "").strip().lower() in {
                "bash", "shell", "terminal", "browser", "feishu_cli", "optional_abilities",
                "tongxin_cli", "agent_capability", "mcp", "mcp_server", "write", "edit", "fs_write", "skill_write",
                "env_config", "send", "scheduler", "evolution_undo",
                "web_fetch", "web_search", "vision", "ocr", "imagegen", "image_jobs",
            }
            if (tool_name or "").strip().lower() == "optional_abilities" and str((arguments or {}).get("action") or "").strip().lower() in {"list", "status"}:
                risky = False
            if risky:
                return {"allowed": False, "reason": "Permission broker failed; local external tool execution was blocked."}
            return {"allowed": True, "reason": "permission-check-error"}

    @staticmethod
    def _permission_proxy_for_tool(tool, tool_name: str, arguments: dict) -> Tuple[str, dict]:
        """Map MCP tools onto the same permission categories as first-party tools."""
        server_name = str(getattr(tool, "server_name", "") or "")
        if server_name:
            proxy_name = "browser" if server_name == "chrome-devtools" else "mcp"
            proxy_args = {
                "server": server_name,
                "tool": tool_name,
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
            return proxy_name, proxy_args
        if tool_name == "agent_capability":
            args = arguments if isinstance(arguments, dict) else {}
            action = str(args.get("action") or "").strip().lower()
            if action == "install_pack":
                ability = args.get("pack_id") or args.get("ability") or ""
                normalized_ability = str(ability or "").strip().lower().replace("_", "-")
                if normalized_ability in {"tongxin", "tongxin-cli", "xin-agent", "xin-agent-cli", "tx-assistant"}:
                    script_path = args.get("script_path") or args.get("scriptPath") or args.get("path")
                    proxy_args = {
                        "action": "configure" if script_path else "auto_configure",
                        "scope": "agent_capability_install_pack_preflight",
                    }
                    if script_path:
                        proxy_args["script_path"] = script_path
                    return "tongxin_cli", proxy_args
                if normalized_ability in {"feishu", "lark", "feishu-lark", "lark-feishu"}:
                    ability = "feishu-cli"
                elif normalized_ability in {"feishu-cli", "lark-cli"}:
                    ability = "feishu-cli"
                return "optional_abilities", {
                    "action": "install",
                    "ability": ability,
                }
            if action in {"install_skill", "enable_skill", "disable_skill"}:
                return "skill_write", {
                    "action": action,
                    "name": args.get("skill") or args.get("name") or "",
                    "type": args.get("type") or "",
                }
            if action in {"configure_mcp", "reload_mcp"}:
                server = args.get("server") if isinstance(args.get("server"), dict) else {}
                return "mcp_server", {
                    "action": action,
                    "server_name": server.get("name") or "",
                    "command": server.get("command") or "",
                }
            return "agent_capability", args
        return tool_name, arguments
    
    def _is_thinking_enabled(self) -> bool:
        """Whether deep-thinking mode is on at the model layer.

        Mirrors the global toggle used by ``bridge.agent_bridge`` when deciding
        whether to send ``thinking={"type": "enabled"}`` to the model. Used for
        logging and reasoning-update event emission across all channels.
        """
        from config import conf
        return bool(conf().get("enable_thinking", False))

    def _should_render_thinking_inline(self) -> bool:
        """Whether ``<think>...</think>`` blocks embedded directly in ``content``
        (MiniMax, some third-party proxies) should be surfaced to the channel.

        Only the Web console can render them in a collapsible panel. IM channels
        (WeChat/WeCom/DingTalk/Feishu) must strip them, otherwise users see raw
        XML tags in their chat.
        """
        from config import conf
        channel_type = getattr(self.model, 'channel_type', '') or ''
        return conf().get("enable_thinking", False) and channel_type == 'web'

    def _filter_think_tags(self, text: str) -> str:
        """
        Handle <think>...</think> blocks in content returned by some LLM providers
        (e.g., MiniMax).

        - When inline thinking rendering is allowed (Web + thinking enabled):
          remove only the tags, keep the content inside.
        - Otherwise (IM channels, or thinking disabled globally): remove both
          the tags and the content entirely.
        """
        if not text:
            return text
        import re
        if self._should_render_thinking_inline():
            text = re.sub(r'<think>', '', text)
            text = re.sub(r'</think>', '', text)
        else:
            text = re.sub(r'<think>[\s\S]*?</think>', '', text)
            # Also strip unclosed <think> tag at the end (streaming partial)
            text = re.sub(r'<think>[\s\S]*$', '', text)
        return text

    def _hash_args(self, args: dict) -> str:
        """Generate a simple hash for tool arguments"""
        import hashlib
        # Sort keys for consistent hashing
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(args_str.encode()).hexdigest()[:8]
    
    def _check_consecutive_failures(self, tool_name: str, args: dict) -> Tuple[bool, str, bool]:
        """
        Check if tool has failed too many times consecutively or called repeatedly with same args
        
        Returns:
            (should_stop, reason, is_critical)
            - should_stop: Whether to stop tool execution
            - reason: Reason for stopping
            - is_critical: Whether to abort entire conversation (True for 8+ failures)
        """
        args_hash = self._hash_args(args)
        
        # Count consecutive calls (both success and failure) for same tool + args
        # This catches infinite loops where tool succeeds but LLM keeps calling it
        same_args_calls = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name and ahash == args_hash:
                same_args_calls += 1
            else:
                break  # Different tool or args, stop counting
        
        # Stop at 5 consecutive calls with same args (whether success or failure)
        if same_args_calls >= 5:
            return True, f"工具 '{tool_name}' 使用相同参数已被调用 {same_args_calls} 次，停止执行以防止无限循环。如果需要查看配置，结果已在之前的调用中返回。", False
        
        # Count consecutive failures for same tool + args
        same_args_failures = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name and ahash == args_hash:
                if not success:
                    same_args_failures += 1
                else:
                    break  # Stop at first success
            else:
                break  # Different tool or args, stop counting
        
        if same_args_failures >= 3:
            return True, f"工具 '{tool_name}' 使用相同参数连续失败 {same_args_failures} 次，停止执行以防止无限循环", False
        
        # Count consecutive failures for same tool (any args)
        same_tool_failures = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name:
                if not success:
                    same_tool_failures += 1
                else:
                    break  # Stop at first success
            else:
                break  # Different tool, stop counting
        
        # Hard stop at 8 failures - abort with critical message
        if same_tool_failures >= 8:
            return True, _t(
                "抱歉，我没能完成这个任务。可能是我理解有误或者当前方法不太合适。\n\n建议你：\n• 换个方式描述需求试试\n• 把任务拆分成更小的步骤\n• 或者换个思路来解决",
                "Sorry, I couldn't complete this task. I may have misunderstood, or my current approach isn't quite right.\n\nYou could try:\n• Rephrasing your request\n• Breaking the task into smaller steps\n• Taking a different approach",
            ), True
        
        # Warning at 6 failures
        if same_tool_failures >= 6:
            return True, f"工具 '{tool_name}' 连续失败 {same_tool_failures} 次（使用不同参数），停止执行以防止无限循环", False
        
        return False, "", False
    
    def _record_tool_result(self, tool_name: str, args: dict, success: bool):
        """Record tool execution result for failure tracking"""
        args_hash = self._hash_args(args)
        self.tool_failure_history.append((tool_name, args_hash, success))
        # Keep only last 50 records to avoid memory bloat
        if len(self.tool_failure_history) > 50:
            self.tool_failure_history = self.tool_failure_history[-50:]

        chain_key = self._tool_chain_key(tool_name, args)
        self.tool_chain_history.append((chain_key, tool_name, success))
        if len(self.tool_chain_history) > 50:
            self.tool_chain_history = self.tool_chain_history[-50:]
        if str(tool_name or "").strip().lower() == "imagegen" and success:
            self._current_turn_imagegen_success = True

    @staticmethod
    def _cli_arg_value(cli_args: List[Any], flag: str) -> str:
        """Return a CLI flag value from a token list without shell parsing."""
        if not isinstance(cli_args, list) or not flag:
            return ""
        for index, token in enumerate(cli_args):
            text = str(token)
            if text == flag and index + 1 < len(cli_args):
                return str(cli_args[index + 1]).strip()
            prefix = f"{flag}="
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return ""

    @staticmethod
    def _cli_subcommand(cli_args: List[Any]) -> str:
        if not isinstance(cli_args, list):
            return ""
        for token in cli_args[1:]:
            text = str(token).strip().lower()
            if text.startswith("+"):
                return text
        return ""

    def _feishu_cli_chain_key(self, args: dict) -> str:
        cli_args = args.get("args")
        domain = ""
        if isinstance(cli_args, list) and cli_args:
            domain = str(cli_args[0]).strip().lower()
        domain = domain or str(args.get("domain") or args.get("scope") or "").strip().lower()
        action = str(args.get("action") or "").strip().lower()

        if action == "run" and domain == "im" and isinstance(cli_args, list):
            subcommand = self._cli_subcommand(cli_args) or "command"
            if subcommand == "+chat-messages-list":
                target = (
                    self._cli_arg_value(cli_args, "--chat-id")
                    or self._cli_arg_value(cli_args, "--user-id")
                    or "unknown-target"
                )
                page = self._cli_arg_value(cli_args, "--page-token") or "first-page"
                start = self._cli_arg_value(cli_args, "--start") or "no-start"
                end = self._cli_arg_value(cli_args, "--end") or "no-end"
                sort = self._cli_arg_value(cli_args, "--sort") or "default-sort"
                return f"feishu_cli:{action}:{domain}:{subcommand}:{target}:{page}:{start}:{end}:{sort}"
            if subcommand in {"+chat-list", "+chat-search"}:
                page = self._cli_arg_value(cli_args, "--page-token") or "first-page"
                query = self._cli_arg_value(cli_args, "--query") or self._cli_arg_value(cli_args, "--member-ids")
                sort = self._cli_arg_value(cli_args, "--sort-type") or self._cli_arg_value(cli_args, "--sort")
                return f"feishu_cli:{action}:{domain}:{subcommand}:{query or 'all'}:{page}:{sort or 'default-sort'}"
            return f"feishu_cli:{action}:{domain}:{subcommand}"

        return f"feishu_cli:{action}:{domain}"

    def _tool_chain_key(self, tool_name: str, args: dict) -> str:
        """Group related tool calls so cross-argument loops can be detected."""
        name = (tool_name or "").strip().lower()
        args = args if isinstance(args, dict) else {}
        if name == "feishu_cli":
            return self._feishu_cli_chain_key(args)
        if name == "tongxin_cli":
            action = str(args.get("action") or "").strip().lower() or "status"
            cli_args = args.get("args") if isinstance(args.get("args"), list) else []
            command = ":".join(str(item).strip().lower() for item in cli_args[:2] if str(item).strip())
            return f"tongxin_cli:{action}:{command or 'status'}"
        if name in {"bash", "shell", "terminal"}:
            command = str(args.get("command") or args.get("cmd") or "").strip().lower()
            if self._looks_like_feishu_cli_command(command):
                return "feishu_cli:bash"
            if self._looks_like_tongxin_cli_command(command):
                return "tongxin_cli:bash"
            if self._looks_like_image_generation_shell_command(command):
                return "imagegen:bash"
            if "chrome-devtools" in command or "remote-debugging-port" in command or "cdp" in command:
                return "browser:cdp"
            for prefix in ("python", "powershell", "pwsh", "node", "npm", "npx", "git", "curl"):
                if command.startswith(prefix) or f" {prefix} " in command:
                    return f"bash:{prefix}"
            return "bash:generic"
        if name == "browser":
            return f"browser:{str(args.get('action') or '').strip().lower()}"
        if name == "ocr":
            return f"ocr:{str(args.get('action') or '').strip().lower()}"
        if name.startswith("mcp__chrome-devtools__") or name.startswith("mcp__chrome_devtools__"):
            return "browser:cdp"
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            server = parts[1] if len(parts) > 1 and parts[1] else "server"
            return f"mcp:{server}"
        if name == "host_diagnostics":
            action = str(args.get("action") or "status").strip().lower()
            return f"host_diagnostics:{action or 'status'}"
        if name in {"read", "ls", "web_fetch", "web_search"}:
            return f"{name}:read"
        return name or "unknown"

    def _count_recent_chain(self, chain_key: str) -> int:
        count = 0
        for key, _name, _success in reversed(self.tool_chain_history):
            if key == chain_key:
                count += 1
            else:
                break
        return count

    def _check_tool_chain_budget(self, tool_name: str, args: dict) -> Tuple[bool, str]:
        chain_key = self._tool_chain_key(tool_name, args)
        recent_count = self._count_recent_chain(chain_key)
        if chain_key.startswith("feishu_cli") and recent_count >= 6:
            return True, (
                "Feishu/Lark tool chain has been used repeatedly without converging. "
                "Stop calling Feishu commands now: summarize what is known, state the exact blocker "
                "(auth, empty attachment output, slow page, missing field, or unsupported command), "
                "and ask the user for the next authorization or input if needed."
            )
        if chain_key.startswith("tongxin_cli") and recent_count >= 6:
            return True, (
                "Tongxin CLI read-only query chain has been used repeatedly without converging. "
                "Stop calling Tongxin commands now: summarize the latest account-data result or exact blocker "
                "(missing script, empty result, permission scope, unsupported command, or timeout), then ask the user for the next input."
            )
        if chain_key.startswith("browser:") and recent_count >= 8:
            return True, (
                "Browser/CDP tool chain has been used repeatedly without converging. "
                "Stop browser calls now, summarize the current page state and the blocker, "
                "then ask the user to log in, authorize, or clarify the target."
            )
        if chain_key.startswith("bash:") and recent_count >= 10:
            return True, (
                "Shell tool chain has been used repeatedly without converging. "
                "Stop running more shell commands now, summarize progress and choose a different approach "
                "or ask the user for confirmation."
            )
        return False, ""

    def _build_convergence_hint(self) -> str:
        if not self.tool_chain_history:
            return ""
        chain_key = self.tool_chain_history[-1][0]
        recent_count = self._count_recent_chain(chain_key)
        if recent_count < 4 or self._last_convergence_hint_key == f"{chain_key}:{recent_count}":
            return ""
        if not (
            chain_key.startswith("feishu_cli")
            or chain_key.startswith("browser:")
            or chain_key.startswith("bash:")
        ):
            return ""
        self._last_convergence_hint_key = f"{chain_key}:{recent_count}"
        return (
            f"You have used the same external capability chain '{chain_key}' {recent_count} times in a row. "
            "If enough information has been collected, provide the final answer now. "
            "If it is blocked, name the blocker precisely and ask the user for the required authorization/input. "
            "Do not continue probing the same chain unless the next call is clearly new and necessary."
        )

    def _force_text_response_once(self, reason: str) -> None:
        """Disable tool schemas for the next model turn so loops close in text."""
        self._force_text_response_next_turn = True
        self._force_text_response_reason = reason or "external-capability-loop"

    @staticmethod
    def _assistant_message_text(message: dict) -> str:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return ""

    def _ensure_final_response_message(self, final_response: str) -> None:
        """Persist internally generated final text as the last assistant turn."""
        text = sanitize_assistant_identity((final_response or "").strip())
        if not text:
            return
        latest_assistant_text = ""
        for message in reversed(self.messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                latest_assistant_text = sanitize_assistant_identity(
                    self._assistant_message_text(message)
                ).strip()
                break
        if latest_assistant_text == text:
            return
        self.messages.append({
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": text,
            }],
        })

    def _append_internal_hint(self, text: str) -> None:
        """Add a model-only hint and remove it after the next model turn."""
        hint = (text or "").strip()
        if not hint:
            return
        self._internal_hint_texts.append(hint)
        self.messages.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": hint,
            }],
        })

    def _remove_internal_hints(self) -> None:
        if not self._internal_hint_texts:
            return
        hints = set(self._internal_hint_texts)
        cleaned = []
        removed = 0
        for message in self.messages:
            if message.get("role") == "user":
                content = message.get("content")
                if (
                    isinstance(content, list)
                    and len(content) == 1
                    and isinstance(content[0], dict)
                    and content[0].get("type") == "text"
                    and str(content[0].get("text") or "").strip() in hints
                ):
                    removed += 1
                    continue
            cleaned.append(message)
        if removed:
            self.messages[:] = cleaned
            logger.debug(f"[Agent] Removed {removed} internal hint message(s) from history")
        self._internal_hint_texts.clear()

    def _latest_user_text_for_tool_schema(self) -> str:
        """Return the latest real user text, ignoring tool_result messages."""
        texts = self._recent_real_user_texts(limit=1)
        return texts[0] if texts else ""

    def _recent_real_user_texts(self, limit: int = 4) -> List[str]:
        """Return recent real user texts newest-first, ignoring tool_result messages."""
        texts: List[str] = []
        for message in reversed(self.messages or []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
                    continue
                parts = [
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(parts)
            else:
                continue
            text = str(text or "").strip()
            if not text:
                continue
            texts.append(text)
            if len(texts) >= limit:
                break
        return texts

    @staticmethod
    def _tool_schema_config_bool(name: str, default: bool) -> bool:
        try:
            from config import conf
            value = conf().get(name, default)
        except Exception:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def _tool_schema_group(self, tool_name: str) -> str:
        name = (tool_name or "").strip().lower()
        name = TOOL_NAME_ALIASES.get(name, name)
        if name.startswith("mcp__chrome-devtools__") or name.startswith("mcp__chrome_devtools__"):
            return "browser"
        if name.startswith("mcp__"):
            return "mcp"
        if name == "browser":
            return "browser"
        if name in {"web_search", "web_fetch"}:
            return "web"
        if name == "feishu_cli":
            return "feishu"
        if name == "tongxin_cli":
            return "tongxin"
        if name == "scheduler":
            return "scheduler"
        if name == "subagent":
            return "subagent"
        if name == "vision":
            return "vision"
        if name == "ocr":
            return "ocr"
        if name == "imagegen":
            return "imagegen"
        if name in {"office_documents", "office_pdf", "office_presentations", "office_spreadsheets"}:
            return "office"
        if name in {"memory_search", "memory_get"}:
            return "memory"
        if name in {"host_diagnostics", "optional_abilities", "agent_capability", "ecorex_cli", "env_config"}:
            return "diagnostics"
        return "core" if name in TOOL_SCHEMA_CORE_NAMES else "other"

    @staticmethod
    def _canonical_tool_name(tool_name: str) -> str:
        name = str(tool_name or "").strip()
        return TOOL_NAME_ALIASES.get(name.lower(), name)

    def _tool_schema_intent_groups(self, user_text: str) -> set:
        lowered = (user_text or "").lower()
        groups = set()
        for group, keywords in TOOL_SCHEMA_INTENT_KEYWORDS.items():
            if any(self._intent_keyword_matches(lowered, keyword) for keyword in keywords):
                groups.add(group)
        if self._looks_like_imagegen_user_intent(user_text):
            groups.add("imagegen")
        if "mcp" in lowered:
            groups.add("mcp")
            groups.add("diagnostics")
        return groups

    @staticmethod
    def _looks_like_imagegen_user_intent(user_text: str) -> bool:
        text = str(user_text or "").strip().lower()
        if not text:
            return False
        return any(pattern.search(text) for pattern in IMAGEGEN_INTENT_REGEXES)

    @staticmethod
    def _looks_like_semantic_image_edit_user_intent(user_text: str) -> bool:
        text = str(user_text or "").strip().lower()
        if not text:
            return False
        return any(pattern.search(text) for pattern in IMAGEGEN_SEMANTIC_EDIT_REGEXES)

    @staticmethod
    def _intent_keyword_matches(lowered_text: str, keyword: str) -> bool:
        keyword = str(keyword or "").lower()
        if not keyword:
            return False
        # ASCII words should not match inside larger words: "base" must not
        # select Feishu Base tools for an unrelated "database" question.
        if "_" in keyword:
            return keyword in lowered_text
        if re.fullmatch(r"[a-z0-9_]+(?:[ -][a-z0-9_]+)*", keyword):
            return re.search(
                rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])",
                lowered_text,
            ) is not None
        return keyword in lowered_text

    @staticmethod
    def _is_tool_schema_followup_confirmation(user_text: str) -> bool:
        normalized = re.sub(r"[\s。.!！?？,，]+", " ", str(user_text or "").strip().lower()).strip()
        if not normalized:
            return False
        if normalized in TOOL_SCHEMA_FOLLOWUP_CONFIRMATIONS:
            return True
        return len(normalized) <= 12 and any(word in normalized for word in TOOL_SCHEMA_FOLLOWUP_CONFIRMATIONS)

    @staticmethod
    def _context_budget_config_int(name: str, default: int) -> int:
        try:
            from config import conf
            value = conf().get(name, default)
        except Exception:
            return int(default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _model_retry_config_int(default: int) -> int:
        try:
            from config import conf
            cfg = conf()
            value = cfg.get("model_max_retries", cfg.get("max_model_retries", default))
        except Exception:
            return max(0, int(default))
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return max(0, int(default))

    @staticmethod
    def _context_budget_config_float(name: str, default: float) -> float:
        try:
            from config import conf
            value = conf().get(name, default)
        except Exception:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _estimate_text_tokens_for_budget(self, text: Any) -> int:
        value = str(text or "")
        estimator = getattr(self.agent, "_estimate_text_tokens", None)
        if callable(estimator):
            try:
                return max(0, int(estimator(value)))
            except Exception:
                pass
        if not value:
            return 0
        non_ascii = sum(1 for char in value if ord(char) > 127)
        ascii_count = len(value) - non_ascii
        return int(non_ascii * 1.5 + ascii_count * 0.25) + 1

    def _estimate_payload_tokens_for_budget(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return self._estimate_text_tokens_for_budget(value)
        try:
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            serialized = str(value)
        return self._estimate_text_tokens_for_budget(serialized)

    @staticmethod
    def _is_context_overflow_error(
        message: Any = "",
        status_code: Any = "",
        error_code: Any = "",
        error_type: Any = "",
        taxonomy: Any = "",
    ) -> bool:
        evidence = " ".join(
            str(part or "").lower()
            for part in (message, status_code, error_code, error_type, taxonomy)
            if part is not None
        )
        if "context_overflow" in evidence:
            return True
        return any(keyword in evidence for keyword in CONTEXT_OVERFLOW_KEYWORDS)

    @staticmethod
    def _is_message_format_error_text(message: Any = "") -> bool:
        text = str(message or "").lower()
        return any(keyword in text for keyword in [
            "tool_use", "tool_result", "tool result", "without", "immediately after",
            "corresponding", "must have", "each",
            "tool_call_id", "tool id", "is not found", "not found", "tool_calls",
            "must be a response to a preceeding message",
            "2013",
        ]) and (
            "400" in text
            or "status: 400" in text
            or "invalid_request" in text
            or "invalidparameter" in text
        )

    def _context_budget_limits(self) -> Dict[str, Any]:
        context_window = 128000
        if self.agent and hasattr(self.agent, "_get_model_context_window"):
            try:
                context_window = max(1, int(self.agent._get_model_context_window()))
            except Exception:
                context_window = 128000

        reserve_default = max(1000, int(context_window * 0.1))
        reserve_getter = getattr(self.agent, "_get_context_reserve_tokens", None)
        if callable(reserve_getter):
            try:
                reserve_default = int(reserve_getter())
            except Exception:
                pass
        response_reserve = self._context_budget_config_int(
            "agent_context_budget_response_reserve_tokens",
            reserve_default,
        )
        if response_reserve <= 0:
            response_reserve = reserve_default
        max_window_reserve = max(0, context_window - 1)
        max_ratio_reserve = max_window_reserve if context_window <= 2 else max(1, int(context_window * 0.5))
        max_response_reserve = min(max_window_reserve, max_ratio_reserve)
        response_reserve = min(max(0, response_reserve), max_response_reserve)

        configured_limit = getattr(self.agent, "max_context_tokens", None) if self.agent else None
        try:
            configured_limit = int(configured_limit) if configured_limit else None
        except (TypeError, ValueError):
            configured_limit = None

        window_input_limit = max(1, context_window - response_reserve)
        requested_limit = configured_limit or window_input_limit
        clamp_to_window = self._tool_schema_config_bool("agent_context_budget_clamp_to_window", True)
        effective_limit = min(requested_limit, window_input_limit) if clamp_to_window else requested_limit
        effective_limit = max(1, int(effective_limit))

        return {
            "model": getattr(self.model, "model", "") or "",
            "context_window_tokens": context_window,
            "configured_max_context_tokens": configured_limit,
            "response_reserve_tokens": response_reserve,
            "window_input_limit_tokens": window_input_limit,
            "effective_context_limit_tokens": effective_limit,
            "clamped_to_window": bool(clamp_to_window and configured_limit and configured_limit > window_input_limit),
        }

    def _estimate_message_components_for_budget(self, messages: List[Dict[str, Any]]) -> Dict[str, int]:
        components = {
            "message_tokens": 0,
            "text_tokens": 0,
            "reasoning_tokens": 0,
            "tool_use_tokens": 0,
            "tool_result_tokens": 0,
            "artifact_metadata_tokens": 0,
            "media_tokens": 0,
        }

        for message in messages or []:
            if not isinstance(message, dict):
                continue
            components["message_tokens"] += 4
            content = message.get("content", "")
            if isinstance(content, str):
                tokens = self._estimate_text_tokens_for_budget(content)
                components["text_tokens"] += tokens
                components["message_tokens"] += tokens
                continue
            if not isinstance(content, list):
                tokens = self._estimate_payload_tokens_for_budget(content)
                components["text_tokens"] += tokens
                components["message_tokens"] += tokens
                continue

            for block in content:
                if not isinstance(block, dict):
                    tokens = self._estimate_payload_tokens_for_budget(block)
                    components["text_tokens"] += tokens
                    components["message_tokens"] += tokens
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    tokens = self._estimate_text_tokens_for_budget(block.get("text", ""))
                    components["text_tokens"] += tokens
                elif block_type == "thinking":
                    tokens = self._estimate_text_tokens_for_budget(block.get("thinking", ""))
                    components["reasoning_tokens"] += tokens
                elif block_type == "tool_use":
                    tokens = 50 + self._estimate_payload_tokens_for_budget(block.get("input", {}))
                    components["tool_use_tokens"] += tokens
                elif block_type == "tool_result":
                    payload = block.get("content", "")
                    tokens = 30 + self._estimate_payload_tokens_for_budget(payload)
                    components["tool_result_tokens"] += tokens
                    components["artifact_metadata_tokens"] += self._estimate_artifact_metadata_tokens(payload)
                elif block_type in {"image", "input_image", "image_url"}:
                    tokens = 1200
                    components["media_tokens"] += tokens
                else:
                    tokens = self._estimate_payload_tokens_for_budget(block)
                    components["text_tokens"] += tokens
                components["message_tokens"] += tokens

        return components

    def _estimate_artifact_metadata_tokens(self, payload: Any) -> int:
        value = payload
        if isinstance(payload, str):
            stripped = payload.strip()
            if not stripped or stripped[0] not in "[{":
                return 0
            try:
                value = json.loads(stripped)
            except Exception:
                return 0

        stack = [value]
        tokens = 0
        scanned = 0
        while stack and scanned < 80:
            item = stack.pop()
            scanned += 1
            if isinstance(item, dict):
                item_type = str(item.get("type") or item.get("kind") or "").lower()
                looks_like_artifact = (
                    item_type == "artifact"
                    or "artifact" in item
                    or "artifacts" in item
                    or ("path" in item and any(key in item for key in ("title", "name", "kind", "mime")))
                )
                if looks_like_artifact:
                    metadata = {
                        key: item.get(key)
                        for key in ("id", "type", "kind", "title", "name", "path", "url", "mime", "metadata")
                        if key in item
                    }
                    tokens += self._estimate_payload_tokens_for_budget(metadata)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return tokens

    def _build_context_budget(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        schema_budget: Dict[str, Any],
    ) -> Dict[str, Any]:
        limits = self._context_budget_limits()
        system_tokens = self._estimate_text_tokens_for_budget(self.system_prompt)
        message_components = self._estimate_message_components_for_budget(messages)
        tool_schema_tokens = self._estimate_payload_tokens_for_budget(tools_schema or [])
        runtime_artifact_tokens = self._estimate_payload_tokens_for_budget(self.files_to_send or [])
        artifact_metadata_tokens = message_components.get("artifact_metadata_tokens", 0) + runtime_artifact_tokens
        estimated_input_tokens = (
            system_tokens
            + message_components.get("message_tokens", 0)
            + tool_schema_tokens
            + runtime_artifact_tokens
        )
        effective_limit = max(1, int(limits["effective_context_limit_tokens"]))
        warn_ratio = self._context_budget_config_float(
            "agent_context_budget_warn_ratio",
            CONTEXT_BUDGET_WARN_RATIO_DEFAULT,
        )
        warn_ratio = min(max(0.1, warn_ratio), 0.99)
        usage_ratio = estimated_input_tokens / float(effective_limit)
        severity = "over_budget" if estimated_input_tokens > effective_limit else (
            "near_limit" if usage_ratio >= warn_ratio else "ok"
        )
        turns = self._identify_complete_turns()
        current_turn_tokens = self._estimate_message_components_for_budget(
            turns[-1]["messages"] if turns else []
        ).get("message_tokens", 0)

        return {
            "enabled": True,
            **limits,
            "warning_ratio": warn_ratio,
            "severity": severity,
            "over_budget": severity == "over_budget",
            "near_limit": severity in {"near_limit", "over_budget"},
            "estimated_input_tokens": estimated_input_tokens,
            "remaining_input_tokens": effective_limit - estimated_input_tokens,
            "usage_ratio": round(usage_ratio, 4),
            "system_prompt_tokens": system_tokens,
            "message_tokens": message_components.get("message_tokens", 0),
            "text_tokens": message_components.get("text_tokens", 0),
            "reasoning_tokens": message_components.get("reasoning_tokens", 0),
            "tool_use_tokens": message_components.get("tool_use_tokens", 0),
            "tool_result_tokens": message_components.get("tool_result_tokens", 0),
            "tool_schema_tokens": tool_schema_tokens,
            "artifact_metadata_tokens": artifact_metadata_tokens,
            "media_tokens": message_components.get("media_tokens", 0),
            "message_count": len(messages or []),
            "turn_count": len(turns),
            "current_turn_tokens": current_turn_tokens,
            "current_turn_preserved": bool(turns),
            "tool_schema_count": len(tools_schema or []),
            "tool_schema_selected_count": (schema_budget or {}).get("selected_count", 0),
            "tool_schema_deferred_count": (schema_budget or {}).get("deferred_count", 0),
            "runtime_artifact_count": len(self.files_to_send or []),
        }

    def _select_tools_for_schema(
        self,
        force_text_response: bool = False,
        force_text_reason: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        if force_text_response or not self.tools:
            budget = {
                "enabled": False,
                "reason": "forced_text" if force_text_response else "no_tools",
                "selected_count": 0,
                "deferred_count": len(self.tools or {}),
                "selected_tools": [],
                "deferred_tools": sorted((self.tools or {}).keys()),
            }
            if force_text_response and force_text_reason:
                budget["force_text_reason"] = force_text_reason
            return None, budget

        if not self._tool_schema_config_bool("agent_tool_schema_budget_enabled", TOOL_SCHEMA_BUDGET_ENABLED_DEFAULT):
            all_tools = dict(self.tools)
            return all_tools, {
                "enabled": False,
                "reason": "disabled_by_config",
                "selected_count": len(all_tools),
                "deferred_count": 0,
                "selected_tools": sorted(all_tools.keys()),
                "deferred_tools": [],
            }

        user_text = self._latest_user_text_for_tool_schema()
        lowered = user_text.lower()
        intent_groups = self._tool_schema_intent_groups(user_text)
        imagegen_intent = "imagegen" in intent_groups
        imagegen_available = "imagegen" in {str(name or "").strip().lower() for name in (self.tools or {})}
        tool_router = ToolRouterPolicy()
        imagegen_companion_groups = set()
        inherited_followup_intent = False
        if self._is_tool_schema_followup_confirmation(user_text):
            for historical_text in self._recent_real_user_texts(limit=4)[1:]:
                historical_groups = self._tool_schema_intent_groups(historical_text)
                if historical_groups:
                    intent_groups.update(historical_groups)
                    imagegen_intent = imagegen_intent or "imagegen" in historical_groups
                    inherited_followup_intent = True
                    break
        if imagegen_intent:
            imagegen_companion_groups = tool_router.companion_groups_for_imagegen(intent_groups)
        selected: Dict[str, Any] = {}
        reasons: Dict[str, str] = {}
        has_deferred_mcp = any(str(name or "").lower().startswith("mcp__") for name in self.tools)

        for name, tool in (self.tools or {}).items():
            lowered_name = (name or "").strip().lower()
            group = self._tool_schema_group(lowered_name)
            explicit_tool_name = bool(
                lowered_name
                and (
                    lowered_name in lowered
                    or lowered_name.replace("_", "-") in lowered
                    or lowered_name.replace("_", " ") in lowered
                )
            )
            if imagegen_intent:
                if imagegen_available:
                    if lowered_name == "imagegen":
                        selected[name] = tool
                        reasons[name] = "imagegen_primary_route"
                    elif tool_router.allows_imagegen_companion_tool(lowered_name, group, imagegen_companion_groups):
                        selected[name] = tool
                        reasons[name] = f"imagegen_companion:{group}"
                    continue
                if lowered_name in IMAGEGEN_PRIORITY_TOOL_NAMES:
                    selected[name] = tool
                    reasons[name] = "imagegen_visibility_diagnostics"
                elif tool_router.allows_imagegen_companion_tool(lowered_name, group, imagegen_companion_groups):
                    selected[name] = tool
                    reasons[name] = f"imagegen_companion:{group}"
                continue
            if (
                lowered_name == "feishu_cli"
                and has_deferred_mcp
                and group not in intent_groups
                and not explicit_tool_name
            ):
                continue
            if lowered_name in TOOL_SCHEMA_CORE_NAMES:
                selected[name] = tool
                reasons[name] = "core"
            elif group in intent_groups:
                selected[name] = tool
                reasons[name] = f"intent:{group}"
            elif explicit_tool_name:
                selected[name] = tool
                reasons[name] = "explicit_tool_name"
            elif lowered_name.startswith("mcp__"):
                public_parts = lowered_name.replace("__", " ").replace("_", " ")
                if public_parts and public_parts in lowered:
                    selected[name] = tool
                    reasons[name] = "explicit_mcp_name"

        if imagegen_intent and not selected:
            deferred = {name: tool for name, tool in self.tools.items()}
            return {}, {
                "enabled": True,
                "reason": "imagegen_intent_no_safe_schema_tool",
                "intent_groups": sorted(intent_groups),
                "inherited_followup_intent": inherited_followup_intent,
                "imagegen_intent": True,
                "imagegen_available": False,
                "selected_count": 0,
                "deferred_count": len(deferred),
                "selected_tools": [],
                "deferred_tools": sorted(deferred.keys()),
                "selection_reasons": reasons,
            }

        if imagegen_intent:
            selected_groups = {self._tool_schema_group((name or "").strip().lower()) for name in selected}
            if imagegen_companion_groups.difference(selected_groups):
                for name, tool in (self.tools or {}).items():
                    lowered_name = (name or "").strip().lower()
                    if lowered_name in IMAGEGEN_PRIORITY_TOOL_NAMES and name not in selected:
                        selected[name] = tool
                        reasons[name] = "imagegen_companion_diagnostics"
            deferred = {name: tool for name, tool in self.tools.items() if name not in selected}
            selection_meta = tool_router.selection_metadata(selected, deferred, reasons)
            return selected, {
                "enabled": True,
                "reason": "imagegen_intent_primary_route",
                "intent_groups": sorted(intent_groups),
                "companion_intent_groups": sorted(imagegen_companion_groups),
                "inherited_followup_intent": inherited_followup_intent,
                "imagegen_intent": True,
                "imagegen_available": imagegen_available,
                **selection_meta,
            }

        if not imagegen_intent:
            recent_names = {name for _chain, name, _success in self.tool_chain_history[-4:]}
            for name in recent_names:
                if name in self.tools:
                    selected[name] = self.tools[name]
                    reasons[name] = "recent_tool_chain"

        if len(self.tools) <= 8 and not has_deferred_mcp:
            for name, tool in self.tools.items():
                lowered_name = (name or "").strip().lower()
                if self._tool_schema_group(lowered_name) == "other" and name not in selected:
                    selected[name] = tool
                    reasons[name] = "small_custom_toolset"

        if not selected:
            if len(self.tools) <= 8 and not has_deferred_mcp:
                for name, tool in self.tools.items():
                    selected[name] = tool
                    reasons[name] = "small_custom_toolset"
            else:
                first_name = next(
                    (name for name in self.tools if str(name or "").strip().lower() != "feishu_cli"),
                    next(iter(self.tools)),
                )
                selected[first_name] = self.tools[first_name]
                reasons[first_name] = "fallback_first_tool"

        deferred = {name: tool for name, tool in self.tools.items() if name not in selected}
        return selected, {
            "enabled": True,
            "reason": "budgeted",
            "intent_groups": sorted(intent_groups),
            "inherited_followup_intent": inherited_followup_intent,
            "imagegen_intent": imagegen_intent,
            "imagegen_available": imagegen_available,
            "selected_count": len(selected),
            "deferred_count": len(deferred),
            "selected_tools": sorted(selected.keys()),
            "deferred_tools": sorted(deferred.keys()),
            "selection_reasons": reasons,
        }

    def _tool_result_user_action_blocker(self, tool_name: str, payload: Any) -> str:
        """Return a convergence blocker that should force a text-only turn."""
        name = (tool_name or "").strip().lower()
        if name == "imagegen" and isinstance(payload, dict):
            if payload.get("error") or payload.get("code") or (payload.get("failedCount") and not payload.get("successCount")):
                next_action = str(payload.get("nextAction") or payload.get("next_action") or "configure_model_provider")
                return (
                    "The native imagegen route has returned a blocker. Stop calling tools now. "
                    "Do not fall back to shell/Python/PIL/SVG/canvas, web search, or network image scraping. "
                    "Tell the user the exact imagegen blocker and next action "
                    f"({next_action}); image generation must be retried only through `imagegen` after that blocker is fixed."
                )
            return ""
        if name != "feishu_cli" or not isinstance(payload, dict):
            return ""
        if payload.get("authRequired") is True:
            return (
                "Feishu authorization has been started and requires user action. "
                "Stop calling tools now, show the authorization instruction/link already returned by feishu_cli, "
                "and ask the user to continue after authorization is complete."
            )
        if payload.get("available") is False:
            return (
                "Feishu CLI is unavailable in this runtime. Stop probing through bash; explain the missing CLI/setup "
                "state and ask the user to install, enable, or authorize the packaged Feishu CLI path."
            )
        return ""

    @staticmethod
    def _looks_like_image_generation_shell_command(command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return False
        direct_markers = (
            "skills/image-generation/scripts/generate.py",
            "image-generation/scripts/generate.py",
            "gpt-image",
            "image-2-pro",
            "/images/generations",
            "/images/edits",
            "openai.images",
            "client.images.generate",
            "client.images.edit",
            "image_generation",
            "imagegen",
        )
        if any(marker in text for marker in direct_markers):
            return True
        basename = ""
        try:
            first_token = shlex.split(str(command or ""), posix=False)[0]
            basename = AgentStreamExecutor._shell_token_basename(first_token)
        except Exception:
            basename = ""
        if basename in {"python", "python.exe", "python3", "py", "py.exe", "node", "node.exe"}:
            semantic_generation_signal = any(pattern.search(text) for pattern in IMAGEGEN_SHELL_SEMANTIC_SIGNAL_REGEXES)
            if (
                ("from pil import" in text or "imagedraw" in text or "image.new(" in text)
                and semantic_generation_signal
            ):
                return True
            if any(marker in text for marker in ("svg", "canvas", "base64.b64decode")) and semantic_generation_signal:
                return True
        return False

    def _current_turn_text(self) -> str:
        candidates = [self._current_user_message_text]
        for message in reversed(self.messages or []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                text = str(content or "")
            if text.strip():
                candidates.append(text)
                break
        return "\n\n".join(part for part in candidates if part)

    def _current_turn_is_image_retouch(self) -> bool:
        text = self._current_turn_text().lower()
        if not text:
            return False
        retouch_markers = (
            "精准修图",
            "精修标注",
            "retouch-marker",
            "image-retouch",
            "局部修图",
            "标注图附件",
            "箭头尖端",
            "语义图片编辑",
        )
        if any(marker.lower() in text for marker in retouch_markers):
            return True
        if self._looks_like_semantic_image_edit_user_intent(text):
            return True
        retouch_patterns = (
            r"(?:把|将).{0,16}(?:改成|改为|替换成|替换|换成).{0,16}(?:图|图片|图像|海报|画面|文字|错字|单字|一个字)",
            r"(?:图|图片|图像|海报|画面).{0,24}(?:字|文字|错字|单字|一个字).{0,24}(?:改成|改为|替换成|替换|换成)",
            r"(?:把|将).{0,8}(?:图|图片|图像|海报|画面).{0,24}(?:字|文字|错字|单字|一个字).{0,24}(?:改成|改为|替换成|替换|换成)",
        )
        return any(re.search(pattern, text) for pattern in retouch_patterns)

    def _retouch_shell_postprocess_allowed(self, command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return False
        if not self._current_turn_imagegen_success:
            return False
        deterministic_prefixes = (
            "cp ", "copy ", "copy-item ", "mv ", "move ", "move-item ", "ren ", "rename ",
            "rename-item ", "mkdir ", "new-item ", "zip ", "tar ", "7z ", "compress-archive ",
            "sha256sum ", "shasum ", "certutil ",
        )
        return text.startswith(deterministic_prefixes)

    @staticmethod
    def _looks_like_semantic_image_edit_shell_command(command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return False
        script_markers = (
            "from pil import",
            "imagedraw",
            "image.open(",
            "image.new(",
            "cv2.",
            "opencv",
            "magick ",
            "convert ",
            "composite ",
            "drawtext",
            "fill ",
            "annotate",
            "inpaint",
            "mask",
            "crop",
            "paste(",
            "textbbox",
            "truetype",
            "canvas",
            "svg",
        )
        if any(marker in text for marker in script_markers):
            return True
        try:
            basename = AgentStreamExecutor._shell_token_basename(shlex.split(str(command or ""), posix=False)[0])
        except Exception:
            basename = ""
        return basename in {"python", "python.exe", "python3", "py", "py.exe", "node", "node.exe"}

    def _sleep_cancelable(self, seconds: float) -> None:
        """Sleep in short slices so user cancel interrupts retry backoff."""
        deadline = time.time() + max(0, seconds)
        while time.time() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise AgentCancelledError("Agent execution cancelled")
            time.sleep(min(0.25, max(0, deadline - time.time())))

    def _external_capability_reroute(self, tool_name: str, args: dict) -> str:
        """Return a hard-stop message when the model is using the wrong host path."""
        name = (tool_name or "").strip().lower()
        if name not in {"bash", "shell", "terminal"}:
            return ""
        command = str((args or {}).get("command") or (args or {}).get("cmd") or "").strip().lower()
        if not command:
            return ""
        if self._current_turn_is_image_retouch() and not self._retouch_shell_postprocess_allowed(command):
            if "imagegen" in self.tools:
                return (
                    "This is an EcoreX 精准修图 / semantic image-editing task. "
                    "Do not use bash, Python, PIL, OpenCV, ImageMagick, SVG/canvas, or coordinate scripts "
                    "to edit the picture locally. Use the native `imagegen` tool for the actual image edit, "
                    "using the annotated marker image and the original image path as inputs. "
                    "Shell is allowed only after a successful imagegen result for deterministic post-processing "
                    "such as copy, rename, zip, checksum, or reveal."
                )
            return (
                "This is an EcoreX 精准修图 / semantic image-editing task, but `imagegen` is not visible in the current tool table. "
                "Do not fall back to bash/Python/PIL/OpenCV/ImageMagick local editing. Inspect capability visibility with "
                "`host_diagnostics`, `optional_abilities`, or `agent_capability`, then report the exact blocker if image editing cannot run."
            )
        if self._looks_like_feishu_cli_command(command):
            if "feishu_cli" in self.tools:
                return (
                    "Do not call Feishu/Lark CLI through raw bash. Use the `feishu_cli` tool first "
                    "so EcoreX can handle packaged CLI resolution, auth, timeouts, and safe output. "
                    "For first-time CLI app configuration or user-scope authorization, call `feishu_cli` "
                    "with action `agent_auth` so the tool can inspect official diagnostics before choosing "
                    "the displayed Feishu authorization flow."
                )
            if "host_diagnostics" in self.tools:
                return (
                    "Do not keep probing Feishu/Lark CLI through raw bash. Call `host_diagnostics` with "
                    "action `status` first to inspect whether Feishu CLI is packaged and authorized."
                )
        if self._looks_like_tongxin_cli_command(command):
            if "tongxin_cli" in self.tools:
                return (
                    "Do not call Tongxin Assistant CLI through raw bash. Use the `tongxin_cli` tool first "
                    "so EcoreX can enforce the all-user read-only command allowlist, bounded timeouts, "
                    "and sanitized output. Write, sync, auth, submit, approve, delete, and permission-changing "
                    "Tongxin commands are not allowed."
                )
            if "host_diagnostics" in self.tools:
                return (
                    "Do not keep probing Tongxin Assistant CLI through raw bash. Call `host_diagnostics` "
                    "with action `status` first to inspect whether the Tongxin read-only CLI is configured."
                )
        if self._looks_like_image_generation_shell_command(command):
            if "imagegen" in self.tools:
                return (
                    "Do not generate or edit images through raw bash, Python, PIL, SVG/canvas, or direct network API scripts. "
                    "The native `imagegen` route is visible in the current tool table; use `imagegen` for semantic image generation. "
                    "For batch or multi-image generation, choose one or more `imagegen` tool calls according to the visible schema "
                    "and the user's requested ordering. Do not invent a local Python fallback or a fixed setup flow. "
                    "The image model route remains `gpt-image-2-pro` by default and may only visibly fall back within the same GPT Image compatible route."
                )
            if "host_diagnostics" in self.tools:
                return (
                    "Image generation must use the native imagegen capability, not raw shell/Python. "
                    "`imagegen` is not visible in the current tool table; inspect capability/tool visibility with "
                    "`host_diagnostics`, `optional_abilities`, or `agent_capability`, then decide from that evidence "
                    "whether the route can be enabled. If it cannot, report the exact blocker instead of trying local image scripts."
                )
        if (
            "chrome-devtools-mcp" in command
            or "remote-debugging-port" in command
            or "http://127.0.0.1:9222" in command
            or "localhost:9222" in command
        ):
            if "host_diagnostics" in self.tools:
                if "browser" in self.tools:
                    return (
                        "CDP is a browser automation path, not a raw shell task. "
                        "Use the `browser` tool directly with action `snapshot`, `navigate`, "
                        "`click`, `fill`, or `get_text`; EcoreX will attach to the configured "
                        "CDP endpoint and reuse the logged-in browser profile. Do not read "
                        "Codex/Chrome plugin SKILL.md files and do not probe 9222 through bash."
                    )
                return (
                    "Do not probe or launch CDP through raw bash as the first browser path. "
                    "Call `host_diagnostics` first to inspect CDP/MCP readiness, then use "
                    "`optional_abilities` to enable/install the needed browser or MCP ability. Use shell only after diagnostics "
                    "show a concrete setup blocker."
                )
        return ""

    @staticmethod
    def _shell_token_basename(token: str) -> str:
        text = str(token or "").strip().strip("\"'")
        return text.replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _is_lark_cli_package(token: str) -> bool:
        text = str(token or "").strip().strip("\"'").lower()
        return (
            text == "@larksuite/cli"
            or text.startswith("@larksuite/cli@")
            or text == "lark-cli"
            or text.startswith("lark-cli@")
        )

    @classmethod
    def _is_lark_cli_runner(cls, token: str) -> bool:
        text = str(token or "").strip().strip("\"'").replace("\\", "/").lower()
        name = cls._shell_token_basename(text)
        return (
            name in {"lark-cli", "lark-cli.cmd", "lark-cli.exe"}
            or (
                text.endswith("/scripts/run.js")
                and ("cli-main/" in text or "@larksuite/cli/" in text or "lark-cli/" in text)
            )
        )

    @staticmethod
    def _looks_like_feishu_cli_command(command: str) -> bool:
        text = str(command or "").strip().lower().replace("\\", "/").replace("%2f", "/").replace("%40", "@")
        if "lark-cli" in text or "@larksuite/cli" in text:
            return True
        if (
            "github.com/larksuite/cli" in text
            or "github.com:larksuite/cli" in text
            or "registry.npmmirror.com/@larksuite/cli" in text
        ):
            return True
        if "scripts/run.js" in text and (
            "cli-main/" in text
            or "/@larksuite/cli/" in text
            or "/lark-cli/" in text
        ):
            return True
        return False

    @staticmethod
    def _is_tongxin_cli_runner(token: str) -> bool:
        text = str(token or "").strip().strip("\"'").replace("\\", "/").lower()
        name = text.rsplit("/", 1)[-1]
        return name in {
            "xin_agent_cli.py",
            "xin agent cli.py",
            "xin-agent-cli.py",
            "tongxin_cli.py",
            "tongxin-cli",
            "tongxin-cli.cmd",
            "tongxin-cli.exe",
            "xin-agent-cli",
            "xin-agent-cli.cmd",
            "xin-agent-cli.exe",
        }

    @classmethod
    def _looks_like_tongxin_cli_command(cls, command: str) -> bool:
        text = str(command or "").strip().lower().replace("\\", "/")
        if "xin_agent_cli.py" in text or "xin agent cli.py" in text or "xin-agent-cli.py" in text or "tongxin_cli.py" in text:
            return True
        if "tongxin-cli" in text or "xin-agent-cli" in text:
            return True
        if "/自动报表工具/" in text and "xin_agent" in text:
            return True
        return False

    @staticmethod
    def _has_shell_control_operator(command: str) -> bool:
        """Reject complex shell commands before automatic host-tool rerouting."""
        if not command:
            return False
        if "\n" in command or "\r" in command:
            return True
        for marker in ("&&", "||", "|", ";", ">", "<"):
            if marker in command:
                return True
        # Windows also treats a standalone ampersand as a command separator.
        return " & " in command

    @classmethod
    def _extract_simple_lark_cli_args(cls, command: str) -> Optional[List[str]]:
        """Extract args from simple raw Feishu CLI shell invocations.

        Complex shell constructs deliberately return None so the normal
        hard-stop guidance still applies instead of reinterpreting arbitrary
        shell syntax as trusted Feishu CLI arguments.
        """
        raw = str(command or "").strip()
        if not raw or cls._has_shell_control_operator(raw):
            return None
        try:
            tokens = shlex.split(raw, posix=False)
        except ValueError:
            return None
        tokens = [str(token).strip().strip("\"'") for token in tokens if str(token).strip()]
        if not tokens:
            return None

        for index, token in enumerate(tokens):
            if cls._is_lark_cli_runner(token):
                if index == 0:
                    return tokens[1:]
                if index == 1 and cls._shell_token_basename(tokens[0]) in {"npx", "npx.cmd", "npx.exe", "node", "node.exe"}:
                    return tokens[2:]
                return None
            if cls._shell_token_basename(token) in {"npx", "npx.cmd", "npx.exe"}:
                for pkg_index in range(index + 1, len(tokens)):
                    candidate = tokens[pkg_index]
                    if str(candidate).startswith("-"):
                        continue
                    if cls._is_lark_cli_package(candidate):
                        return tokens[pkg_index + 1:]
                    return None
            if cls._shell_token_basename(token) in {"node", "node.exe"} and index + 1 < len(tokens):
                runner = tokens[index + 1]
                if cls._is_lark_cli_runner(runner):
                    return tokens[index + 2:]
                return None
        return None

    @classmethod
    def _extract_simple_tongxin_cli_args(cls, command: str) -> Optional[List[str]]:
        """Extract args from simple raw Tongxin CLI shell invocations."""
        raw = str(command or "").strip()
        if not raw or cls._has_shell_control_operator(raw):
            return None
        try:
            tokens = shlex.split(raw, posix=False)
        except ValueError:
            return None
        tokens = [str(token).strip().strip("\"'") for token in tokens if str(token).strip()]
        if not tokens:
            return None

        for index, token in enumerate(tokens):
            if cls._is_tongxin_cli_runner(token):
                if index == 0:
                    return tokens[1:]
                launcher = cls._shell_token_basename(tokens[index - 1]) if index > 0 else ""
                if launcher in {"python", "python.exe", "python3", "py", "py.exe", "node", "node.exe"}:
                    return tokens[index + 1:]
                # Windows py -3 xin_agent_cli.py ...
                if index >= 2 and cls._shell_token_basename(tokens[index - 2]) in {"py", "py.exe"} and tokens[index - 1].startswith("-"):
                    return tokens[index + 1:]
                return None
        return None

    @staticmethod
    def _feishu_autoroute_args(lark_args: List[str], original_args: dict) -> dict:
        lowered = [str(item).strip().lower() for item in lark_args]
        routed: Dict[str, Any]
        if lowered[:2] == ["auth", "status"]:
            routed = {"action": "status"}
        elif lowered[:2] == ["auth", "login"]:
            routed = {"action": "auth_login"}
            for idx, value in enumerate(lowered):
                if value == "--scope" and idx + 1 < len(lark_args):
                    routed["scope"] = str(lark_args[idx + 1])
                if value == "--domain" and idx + 1 < len(lark_args):
                    routed["domain"] = str(lark_args[idx + 1])
            if not routed.get("scope") and not routed.get("domain"):
                routed = {"action": "agent_auth"}
        elif lowered[:2] == ["config", "init"]:
            routed = {"action": "config_init"}
            for idx, value in enumerate(lowered):
                if value == "--brand" and idx + 1 < len(lark_args):
                    routed["brand"] = str(lark_args[idx + 1])
                if value == "--app-id" and idx + 1 < len(lark_args):
                    routed["app_id"] = str(lark_args[idx + 1])
        else:
            routed = {"action": "run", "args": lark_args}
        if isinstance(original_args, dict) and original_args.get("timeout") is not None:
            routed["timeout"] = original_args.get("timeout")
        return routed

    @staticmethod
    def _tongxin_autoroute_args(cli_args: List[str], original_args: dict) -> dict:
        lowered = [str(item).strip().lower() for item in cli_args]
        if lowered[:1] == ["schema"]:
            routed: Dict[str, Any] = {"action": "schema"}
        else:
            routed = {"action": "run", "args": cli_args}
        if isinstance(original_args, dict) and original_args.get("timeout") is not None:
            routed["timeout"] = original_args.get("timeout")
        return routed

    def _external_capability_autoroute(self, tool_name: str, args: dict) -> Tuple[str, dict, str]:
        """Map a simple wrong host path to the safer first-party host tool."""
        name = (tool_name or "").strip().lower()
        if name not in {"bash", "shell", "terminal"}:
            return "", {}, ""
        command = str((args or {}).get("command") or (args or {}).get("cmd") or "").strip()
        if "tongxin_cli" in self.tools:
            tongxin_args = self._extract_simple_tongxin_cli_args(command)
            if tongxin_args is not None:
                return (
                    "tongxin_cli",
                    self._tongxin_autoroute_args(tongxin_args, args or {}),
                    "raw bash tongxin-cli",
                )
        if "feishu_cli" in self.tools:
            lark_args = self._extract_simple_lark_cli_args(command)
            if lark_args is not None:
                return (
                    "feishu_cli",
                    self._feishu_autoroute_args(lark_args, args or {}),
                    "raw bash lark-cli",
                )
        return "", {}, ""

    def run_stream(self, user_message: str) -> str:
        """
        Execute streaming reasoning loop
        
        Args:
            user_message: User message
            
        Returns:
            Final response text
        """
        # Log user message with model info. Truncate very long messages (e.g.
        # injected transcripts / large prompts) so logs stay readable.
        thinking_enabled = self._is_thinking_enabled()
        thinking_label = " | 💭 thinking" if thinking_enabled else ""
        _log_msg = user_message if len(user_message) <= 500 else (
            user_message[:500] + f" …(+{len(user_message) - 500} chars)"
        )
        logger.info(f"🤖 {self.model.model}{thinking_label} | 👤 {_log_msg}")        
        self._current_user_message_text = str(user_message or "")
        
        # Add user message (Claude format - use content blocks for consistency)
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_message
                }
            ]
        })

        # Trim context ONCE before the agent loop starts, not during tool steps.
        # This ensures tool_use/tool_result chains created during the current run
        # are never stripped mid-execution (which would cause LLM loops).
        self._trim_messages()

        # Validate after trimming: trimming may leave orphaned tool_use at the
        # boundary (e.g. the last kept turn ends with an assistant tool_use whose
        # tool_result was in a discarded turn).
        self._validate_and_fix_messages()

        self._emit_event("agent_start")

        final_response = ""
        turn = 0

        cancelled = False
        try:
            while turn < self.max_turns:
                # Check at the very top of every turn so a cancel arriving
                # between turns short-circuits cleanly.
                self._check_cancelled()

                turn += 1
                logger.info(f"[Agent] Turn {turn}")
                self._emit_event("turn_start", {"turn": turn})

                # Call LLM (enable retry_on_empty for better reliability)
                assistant_msg, tool_calls = self._call_llm_stream(retry_on_empty=True)
                self._remove_internal_hints()
                final_response = assistant_msg

                # No tool calls, end loop
                if not tool_calls:
                    # 检查是否返回了空响应
                    if not assistant_msg:
                        logger.warning(f"[Agent] LLM returned empty response after retry (no content and no tool calls)")
                        logger.info(f"[Agent] This usually happens when LLM thinks the task is complete after tool execution")
                        
                        # 如果之前有工具调用，强制要求 LLM 生成文本回复
                        if turn > 1:
                            logger.info(f"[Agent] Requesting explicit response from LLM...")
                            
                            # Remember position so we can remove the injected prompt later
                            prompt_insert_idx = len(self.messages)
                            
                            # 添加一条消息，明确要求回复用户
                            self.messages.append({
                                "role": "user",
                                "content": [{
                                    "type": "text",
                                    "text": "请向用户说明刚才工具执行的结果或回答用户的问题。"
                                }]
                            })
                            
                            # 再调用一次 LLM
                            assistant_msg, tool_calls = self._call_llm_stream(retry_on_empty=False)
                            self._remove_internal_hints()
                            final_response = assistant_msg
                            
                            # Remove the injected prompt from history so it doesn't
                            # appear as a user message in persisted conversations.
                            # _call_llm_stream may have appended an assistant message
                            # after the prompt, so we locate and remove only the prompt.
                            if (prompt_insert_idx < len(self.messages)
                                    and self.messages[prompt_insert_idx].get("role") == "user"):
                                self.messages.pop(prompt_insert_idx)
                                logger.debug("[Agent] Removed injected explicit-response prompt from message history")
                            
                            # If LLM responded with tool_calls instead of text, fall through
                            # to the tool execution path below (don't break the loop).
                            if tool_calls:
                                logger.info(
                                    f"[Agent] LLM returned tool_calls in explicit-response retry, "
                                    f"continuing to execute tools instead of breaking"
                                )
                            elif not assistant_msg:
                                # Still empty (no text and no tool_calls): use fallback
                                logger.warning(f"[Agent] Still empty after explicit request")
                                final_response = _t(
                                    "抱歉，我暂时无法生成回复。请尝试换一种方式描述你的需求，或稍后再试。",
                                    "Sorry, I can't generate a reply right now. Please try rephrasing your request, or try again later.",
                                )
                                logger.info(f"Generated fallback response for empty LLM output")
                        else:
                            # First-turn empty reply, fall back directly
                            final_response = _t(
                                "抱歉，我暂时无法生成回复。请尝试换一种方式描述你的需求，或稍后再试。",
                                "Sorry, I can't generate a reply right now. Please try rephrasing your request, or try again later.",
                            )
                            logger.info(f"Generated fallback response for empty LLM output")
                    else:
                        logger.info(f"💭 {assistant_msg[:150]}{'...' if len(assistant_msg) > 150 else ''}")
                    
                    # If the explicit-response retry produced tool_calls, skip the break
                    # and continue down to the tool execution branch in this same iteration.
                    if not tool_calls:
                        logger.debug(f"✅ Done (no tool calls)")
                        self._emit_event("turn_end", {
                            "turn": turn,
                            "has_tool_calls": False
                        })
                        break

                # Log tool calls with arguments (truncate long values like base64)
                tool_calls_str = []
                for tc in tool_calls:
                    args = tc.get('arguments') or {}
                    if isinstance(args, dict):
                        parts = []
                        for k, v in args.items():
                            parts.append(f"{k}={_safe_tool_arg_log_value(k, v)}")
                        args_str = ', '.join(parts)
                        if args_str:
                            tool_calls_str.append(f"{tc['name']}({args_str})")
                        else:
                            tool_calls_str.append(tc['name'])
                    else:
                        tool_calls_str.append(tc['name'])
                logger.info(f"🔧 {', '.join(tool_calls_str)}")

                # Execute tools
                tool_results = []
                tool_result_blocks = []

                try:
                    for tool_call in tool_calls:
                        # Honour cancel between tool invocations within the same turn
                        self._check_cancelled()
                        result = self._execute_tool(tool_call)
                        tool_results.append(result)
                        
                        # Debug: Check if tool is being called repeatedly with same args
                        if turn > 2:
                            # Check last N tool calls for repeats
                            repeat_count = sum(
                                1 for name, ahash, _ in self.tool_failure_history[-10:]
                                if name == tool_call["name"] and ahash == self._hash_args(tool_call["arguments"])
                            )
                            if repeat_count >= 3:
                                logger.warning(
                                    f"⚠️  Tool '{tool_call['name']}' has been called {repeat_count} times "
                                    f"with same arguments. This may indicate a loop."
                                )
                        
                        # Check if this is a file to send
                        if result.get("status") == "success" and isinstance(result.get("result"), dict):
                            result_data = result.get("result")
                            if result_data.get("type") == "file_to_send":
                                self.files_to_send.append(result_data)
                                logger.info(f"📎 File queued for sending: {result_data.get('file_name', result_data.get('path'))}")
                                self._emit_event("file_to_send", result_data)
                        
                        # Check for critical error - abort entire conversation
                        if result.get("status") == "critical_error":
                            logger.error(f"💥 Fatal error detected, aborting conversation")
                            final_response = result.get('result') or _t("任务执行失败", "Task execution failed")
                            return final_response
                        
                        # Log tool result in compact format
                        status_emoji = "✅" if result.get("status") == "success" else "❌"
                        result_data = result.get('result', '')
                        result_log_preview = _safe_tool_result_log_preview(result_data)
                        logger.info(f"  {status_emoji} {tool_call['name']} ({result.get('execution_time', 0):.2f}s): {result_log_preview}")

                        # Build tool result block (Claude format)
                        # Format content in a way that's easy for LLM to understand
                        is_error = result.get("status") == "error"

                        if is_error:
                            # For errors, provide clear error message
                            result_content = f"Error: {result.get('result', 'Unknown error')}"
                        elif isinstance(result.get('result'), dict):
                            # For dict results, use JSON format
                            result_content = json.dumps(result.get('result'), ensure_ascii=False)
                        elif isinstance(result.get('result'), str):
                            # For string results, use directly
                            result_content = result.get('result')
                        else:
                            # Fallback to full JSON
                            result_content = json.dumps(result, ensure_ascii=False)

                        # Truncate excessively large tool results for the current turn
                        # Historical turns will be further truncated in _trim_messages()
                        MAX_CURRENT_TURN_RESULT_CHARS = 50000
                        if len(result_content) > MAX_CURRENT_TURN_RESULT_CHARS:
                            truncated_len = len(result_content)
                            result_content = result_content[:MAX_CURRENT_TURN_RESULT_CHARS] + \
                                f"\n\n[Output truncated: {truncated_len} chars total, showing first {MAX_CURRENT_TURN_RESULT_CHARS} chars]"
                            logger.info(f"📎 Truncated tool result for '{tool_call['name']}': {truncated_len} -> {MAX_CURRENT_TURN_RESULT_CHARS} chars")

                        tool_result_block = {
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": result_content
                        }
                        
                        # Add is_error field for Claude API (helps model understand failures)
                        if is_error:
                            tool_result_block["is_error"] = True
                        
                        tool_result_blocks.append(tool_result_block)
                
                finally:
                    # CRITICAL: Always add tool_result to maintain message history integrity
                    # Even if tool execution fails, we must add error results to match tool_use
                    if tool_result_blocks:
                        # Add tool results to message history as user message (Claude format)
                        self.messages.append({
                            "role": "user",
                            "content": tool_result_blocks
                        })
                        
                        # Detect potential infinite loop: same tool called multiple times with success
                        # If detected, add a hint to LLM to stop calling tools and provide response
                        if turn >= 3 and len(tool_calls) > 0:
                            tool_name = tool_calls[0]["name"]
                            args_hash = self._hash_args(tool_calls[0]["arguments"])
                            
                            # Count recent successful calls with same tool+args
                            recent_success_count = 0
                            for name, ahash, success in reversed(self.tool_failure_history[-10:]):
                                if name == tool_name and ahash == args_hash and success:
                                    recent_success_count += 1
                            
                            # If tool was called successfully 3+ times with same args, add hint to stop loop
                            if recent_success_count >= 3:
                                logger.warning(
                                    f"⚠️  Detected potential loop: '{tool_name}' called {recent_success_count} times "
                                    f"with same args. Adding hint to LLM to provide final response."
                                )
                                self._force_text_response_once("repeated-successful-tool-call")
                                self._append_internal_hint(
                                    "工具已经成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。"
                                )
                        convergence_hint = self._build_convergence_hint()
                        if convergence_hint:
                            logger.warning(f"[Agent] Adding convergence hint: {convergence_hint}")
                            self._append_internal_hint(convergence_hint)
                    elif tool_calls:
                        # If we have tool_calls but no tool_result_blocks (unexpected error),
                        # create error results for all tool calls to maintain message integrity
                        logger.warning("⚠️ Tool execution interrupted, adding error results to maintain message history")
                        emergency_blocks = []
                        for tool_call in tool_calls:
                            emergency_blocks.append({
                                "type": "tool_result",
                                "tool_use_id": tool_call["id"],
                                "content": "Error: Tool execution was interrupted",
                                "is_error": True
                            })
                        self.messages.append({
                            "role": "user",
                            "content": emergency_blocks
                        })

                self._emit_event("turn_end", {
                    "turn": turn,
                    "has_tool_calls": True,
                    "tool_count": len(tool_calls)
                })

            if turn >= self.max_turns:
                logger.warning(f"⚠️  Reached max decision step limit: {self.max_turns}")
                
                # Force model to summarize without tool calls
                logger.info(f"[Agent] Requesting summary from LLM after reaching max steps...")
                
                # Remember position before injecting the prompt so we can remove it later
                prompt_insert_idx = len(self.messages)
                
                # Add a temporary prompt to force summary
                self.messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": f"你已经执行了{turn}个决策步骤，达到了单次运行的最大步数限制。请总结一下你目前的执行过程和结果，告诉用户当前的进展情况。不要再调用工具，直接用文字回复。"
                    }]
                })
                
                # Call LLM one more time to get summary (without retry to avoid loops)
                try:
                    self._force_text_response_once("max-turn-summary")
                    summary_response, summary_tools = self._call_llm_stream(retry_on_empty=False)
                    self._remove_internal_hints()
                    if summary_response:
                        final_response = summary_response
                        logger.info(f"💭 Summary: {summary_response[:150]}{'...' if len(summary_response) > 150 else ''}")
                    else:
                        # Fallback if model still doesn't respond
                        final_response = _t(
                            f"我已经执行了{turn}个决策步骤，达到了单次运行的步数上限。任务可能还未完全完成，建议你将任务拆分成更小的步骤，或者换一种方式描述需求。",
                            f"I've taken {turn} decision steps and reached the per-run limit. The task may not be fully complete — try breaking it into smaller steps, or describe your request differently.",
                        )
                except Exception as e:
                    logger.warning(f"Failed to get summary from LLM: {_public_agent_exception_message('Summary generation failed.', e)}")
                    final_response = _t(
                        f"我已经执行了{turn}个决策步骤，达到了单次运行的步数上限。任务可能还未完全完成，建议你将任务拆分成更小的步骤，或者换一种方式描述需求。",
                        f"I've taken {turn} decision steps and reached the per-run limit. The task may not be fully complete — try breaking it into smaller steps, or describe your request differently.",
                    )
                finally:
                    # Remove the injected user prompt from history to avoid polluting
                    # persisted conversation records. The assistant summary (if any)
                    # was already appended by _call_llm_stream and is kept.
                    if (prompt_insert_idx < len(self.messages)
                            and self.messages[prompt_insert_idx].get("role") == "user"):
                        self.messages.pop(prompt_insert_idx)
                        logger.debug("[Agent] Removed injected max-steps prompt from message history")

        except AgentCancelledError:
            # User-initiated stop: wind down message history cleanly so the
            # next turn is unaffected; channels emit a "cancelled" UI event.
            cancelled = True
            logger.info(f"[Agent] 🛑 Cancelled by user (turn {turn})")
            self._handle_cancelled(final_response)
            if not final_response or not final_response.strip():
                final_response = "_(Cancelled)_"

        except Exception as e:
            public_error = _public_agent_exception_message("Agent execution failed.", e)
            logger.error(f"❌ Agent execution error: {public_error}")
            error_payload = {"error": public_error, "message": public_error}
            error_payload.update(_public_agent_exception_summary(e))
            retry_evidence = getattr(self, "_last_model_retry_evidence", {}) or {}
            if isinstance(retry_evidence, dict):
                error_payload.update(retry_evidence)
            self._emit_event("error", error_payload)
            raise

        finally:
            final_response = final_response.strip() if final_response else final_response
            final_response = sanitize_assistant_identity(final_response)
            if final_response and not cancelled:
                self._ensure_final_response_message(final_response)
            if cancelled:
                # Emit before agent_end so channels can mark UI as cancelled
                self._emit_event("agent_cancelled", {"final_response": final_response})
            logger.info(f"[Agent] 🏁 Done ({turn} turns)" + (" [cancelled]" if cancelled else ""))
            self._emit_event("agent_end", {
                "final_response": final_response,
                "cancelled": cancelled,
                "usage": self.agent.last_usage,
            })

        return final_response

    def _call_llm_stream(self, retry_on_empty=True, retry_count=0, max_retries=3,
                         _overflow_retry: bool = False,
                         _force_text_turn: Optional[bool] = None,
                         _force_text_reason: str = "") -> Tuple[str, List[Dict]]:
        """
        Call LLM with streaming and automatic retry on errors
        
        Args:
            retry_on_empty: Whether to retry once if empty response is received
            retry_count: Current retry attempt (internal use)
            max_retries: Maximum number of retries for API errors
            _overflow_retry: Internal flag indicating this is a retry after context overflow
        
        Returns:
            (response_text, tool_calls)
        """
        self._last_model_retry_evidence = {}
        # Validate and fix message history (e.g. orphaned tool_result blocks).
        # Context trimming is done once in run_stream() before the loop starts,
        # NOT here — trimming mid-execution would strip the current run's
        # tool_use/tool_result chains and cause LLM loops.
        self._validate_and_fix_messages()

        # Prepare messages
        messages = self._prepare_messages()
        turns = self._identify_complete_turns()
        logger.info(f"Sending {len(messages)} messages ({len(turns)} turns) to LLM")

        # Pull in any MCP tools that finished loading since this turn started.
        # Cheap dict reconciliation (microseconds) — lets the agent pick up
        # newly available MCP tools mid-conversation without a session restart.
        try:
            from agent.tools import ToolManager
            manager = ToolManager()
            ensure_mcp = getattr(manager, "ensure_mcp_configured_loaded", None)
            if callable(ensure_mcp):
                ensure_mcp(wait_seconds=0.2)
            manager.sync_mcp_into_agent(self)
        except Exception as e:
            logger.debug(f"[Agent] MCP sync skipped: {_public_agent_exception_message('MCP sync failed.', e)}")

        if _force_text_turn is None:
            force_text_response = self._force_text_response_next_turn
            force_text_reason = self._force_text_response_reason
            if force_text_response:
                self._force_text_response_next_turn = False
                self._force_text_response_reason = ""
                logger.warning(
                    f"[Agent] Tool schemas disabled for one turn to force convergence: {force_text_reason}"
                )
        else:
            force_text_response = bool(_force_text_turn)
            force_text_reason = _force_text_reason

        schema_tools, schema_budget = self._select_tools_for_schema(
            force_text_response=force_text_response,
            force_text_reason=force_text_reason,
        )
        if schema_budget.get("enabled") or schema_budget.get("deferred_count"):
            logger.info(
                "[Agent] Tool schema budget: "
                f"selected={schema_budget.get('selected_count')} "
                f"deferred={schema_budget.get('deferred_count')} "
                f"groups={schema_budget.get('intent_groups', [])}"
            )
            self._emit_event("tool_schema_budget", schema_budget)

        # Prepare tool definitions. Prefer get_json_schema() when it yields
        # real properties (lets tools augment schema at runtime), otherwise
        # fall back to the static `tool.params` (MCP tools rely on this).
        tools_schema = None
        if schema_tools:
            tools_schema = []
            for tool in schema_tools.values():
                input_schema = tool.params
                try:
                    dynamic = (tool.get_json_schema() or {}).get("parameters") or {}
                    if dynamic.get("properties"):
                        input_schema = dynamic
                except Exception:
                    pass
                tools_schema.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": input_schema,
                })

        context_budget = self._build_context_budget(messages, tools_schema, schema_budget)
        if context_budget.get("near_limit"):
            logger.warning(
                "[Agent] Context budget %s: estimated=%s limit=%s remaining=%s",
                context_budget.get("severity"),
                context_budget.get("estimated_input_tokens"),
                context_budget.get("effective_context_limit_tokens"),
                context_budget.get("remaining_input_tokens"),
            )
        self._emit_event("context_budget", context_budget)

        # Debug: dump the full system prompt and messages sent to the LLM.
        # Gated behind `debug` config to avoid flooding normal logs.
        # try:
        #     from config import conf
        #     if conf().get("debug", False):
        #         logger.debug(
        #             "[Agent][debug] system_prompt sent to LLM "
        #             f"({len(self.system_prompt or '')} chars):\n"
        #             "================ SYSTEM PROMPT BEGIN ================\n"
        #             f"{self.system_prompt}\n"
        #             "================ SYSTEM PROMPT END =================="
        #         )
        #         logger.info(f"[Agent][debug] messages sent to LLM: {messages}")
        # except Exception:
        #     pass

        # Create request
        configured_model_max_retries = self._model_retry_config_int(max_retries)
        request = LLMRequest(
            messages=messages,
            temperature=0,
            stream=True,
            tools=tools_schema,
            system=self.system_prompt,  # Pass system prompt separately for Claude API
            retry_count=retry_count,
            max_model_retries=configured_model_max_retries,
            model_max_retries=configured_model_max_retries,
            model_retry_sleep=self._sleep_cancelable,
            tool_schema_budget=schema_budget,
            context_budget=context_budget,
        )

        self._emit_event("message_start", {"role": "assistant"})

        # Streaming response
        full_content = ""
        full_reasoning = ""
        tool_calls_buffer = {}  # {index: {id, name, arguments}}
        gemini_raw_parts = None  # Preserve Gemini thoughtSignature for round-trip
        stop_reason = None  # Track why the stream stopped
        self.agent.last_usage = None

        try:
            stream = self.model.call_stream(request)

            # Probe cancel every N chunks to bound reaction time without
            # checking on every token.
            _cancel_probe_counter = 0
            _CANCEL_PROBE_EVERY = 8

            for chunk in stream:
                _cancel_probe_counter += 1
                if _cancel_probe_counter >= _CANCEL_PROBE_EVERY:
                    _cancel_probe_counter = 0
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        # Persist partial text only; tool_use args may be
                        # truncated mid-stream and would fail validation.
                        logger.info("[Agent] cancel detected mid-stream, aborting LLM call")
                        if full_content:
                            partial_msg = {
                                "role": "assistant",
                                "content": [{"type": "text", "text": full_content}],
                            }
                            self.messages.append(partial_msg)
                        self._emit_event("message_end", {
                            "content": full_content,
                            "tool_calls": [],
                            "cancelled": True,
                            "usage": self.agent.last_usage,
                        })
                        raise AgentCancelledError("cancelled during LLM streaming")

                if isinstance(chunk, dict):
                    usage = _normalize_usage(chunk.get("usage"), getattr(self.model, "model", ""))
                    if usage:
                        self.agent.last_usage = usage

                # Check for errors
                if isinstance(chunk, dict) and chunk.get("error"):
                    # Extract error message from nested structure
                    error_data = chunk.get("error", {})
                    if isinstance(error_data, dict):
                        error_msg = error_data.get("message", chunk.get("message", "Unknown error"))
                        error_code = error_data.get("code", "")
                        error_type = error_data.get("type", "")
                        error_taxonomy = (
                            error_data.get("taxonomy")
                            or error_data.get("error_taxonomy")
                            or chunk.get("error_taxonomy", "")
                        )
                    else:
                        error_msg = chunk.get("message", str(error_data))
                        error_code = ""
                        error_type = ""
                        error_taxonomy = chunk.get("error_taxonomy", "")
                    
                    status_code = chunk.get("status_code", "N/A")
                    retry_stopped = bool(chunk.get("retry_exhausted") or chunk.get("retry_suppressed"))
                    nested_error = error_data if isinstance(error_data, dict) else {}
                    retry_suppressed = bool(chunk.get("retry_suppressed") or nested_error.get("retry_suppressed"))
                    retry_suppressed_reason = (
                        chunk.get("retry_suppressed_reason")
                        or nested_error.get("retry_suppressed_reason")
                        or ""
                    )
                    retry_exhausted = bool(chunk.get("retry_exhausted") or nested_error.get("retry_exhausted"))
                    retryable = bool(chunk.get("retryable") or nested_error.get("retryable"))
                    retry_attempt = chunk.get("retry_attempt", nested_error.get("retry_attempt"))
                    max_model_retry_attempts = chunk.get("max_retries", nested_error.get("max_retries"))
                    terminal_reason = (
                        "model_retry_suppressed_stream_output_started"
                        if retry_suppressed
                        else "model_retry_exhausted"
                        if retry_exhausted
                        else "model_stream_error"
                    )
                    self._last_model_retry_evidence = {
                        "error_code": "MODEL_RETRY_SUPPRESSED" if retry_suppressed else ("MODEL_RETRY_EXHAUSTED" if retry_exhausted else "MODEL_STREAM_ERROR"),
                        "error_type": error_type or error_taxonomy or ("network_error" if retryable else ""),
                        "error_taxonomy": error_taxonomy,
                        "terminal_reason": terminal_reason,
                        "retryable": retryable,
                        "recoverable": True,
                        "retry_exhausted": retry_exhausted,
                        "retry_suppressed": retry_suppressed,
                        "retry_suppressed_reason": retry_suppressed_reason,
                        "retry_attempt": retry_attempt,
                        "max_retries": max_model_retry_attempts,
                        "status_code": status_code,
                        "retry_mode": "manual_retry_prepare" if retryable else "unavailable",
                    }

                    # Log error with all available information
                    logger.error(f"🔴 Stream API Error:")
                    logger.error(f"   Message: {_public_agent_exception_message('Stream API message redacted.', error_msg)}")
                    logger.error(f"   Status Code: {status_code}")
                    logger.error(f"   Error Code: {error_code}")
                    logger.error(f"   Error Type: {error_type}")
                    logger.error(f"   Full chunk: {_public_agent_exception_summary(chunk)}")
                    
                    # Check if this is a context overflow error. Prefer the
                    # shared model taxonomy when present; keep keyword fallback
                    # for providers that only expose free-form text.
                    is_overflow = self._is_context_overflow_error(
                        message=error_msg,
                        status_code=status_code,
                        error_code=error_code,
                        error_type=error_type,
                        taxonomy=error_taxonomy,
                    )
                    explicit_overflow = self._is_context_overflow_error(
                        message="",
                        status_code=status_code,
                        error_code=error_code,
                        error_type=error_type,
                        taxonomy=error_taxonomy,
                    ) or self._is_context_overflow_error(
                        message=error_msg,
                        status_code="",
                        error_code="",
                        error_type="",
                        taxonomy="",
                    )
                    message_format_error = self._is_message_format_error_text(
                        f"{error_msg} {status_code} {error_code} {error_type}"
                    )
                    if is_overflow and message_format_error and not explicit_overflow:
                        is_overflow = False
                    
                    if is_overflow:
                        # Mark as context overflow for special handling
                        raise Exception(f"[CONTEXT_OVERFLOW] {error_msg} (Status: {status_code})")
                    else:
                        # Raise a user-safe message while keeping raw details in logs above.
                        visible_error = _user_visible_llm_error(error_msg, status_code, error_code, error_type)
                        if retry_stopped:
                            raise Exception(f"[MODEL_RETRY_EXHAUSTED] {visible_error}")
                        raise Exception(visible_error)

                # Parse chunk
                if isinstance(chunk, dict) and chunk.get("choices"):
                    choice = chunk["choices"][0]
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        delta = {}
                    message_payload = choice.get("message") or {}
                    if not isinstance(message_payload, dict):
                        message_payload = {}
                    
                    # Capture finish_reason if present
                    finish_reason = choice.get("finish_reason")
                    if finish_reason:
                        stop_reason = finish_reason

                    reasoning_delta = (
                        _model_content_to_text(delta.get("reasoning_content"))
                        or _model_content_to_text(message_payload.get("reasoning_content"))
                    )
                    if reasoning_delta:
                        full_reasoning += reasoning_delta
                        if self._is_thinking_enabled():
                            self._emit_event("reasoning_update", {"delta": reasoning_delta})

                    # Handle text content
                    content_delta = (
                        _model_content_to_text(delta.get("content"))
                        or _model_content_to_text(delta.get("text"))
                        or _model_content_to_text(delta.get("refusal"))
                        or _model_content_to_text(message_payload.get("content"))
                        or _model_content_to_text(message_payload.get("refusal"))
                        or _model_content_to_text(choice.get("text"))
                    )
                    if content_delta:
                        # Filter out <think> tags from content
                        filtered_delta = self._filter_think_tags(content_delta)
                        full_content += filtered_delta
                        if filtered_delta:  # Only emit if there's content after filtering
                            self._emit_event("message_update", {"delta": sanitize_assistant_identity(filtered_delta)})

                    # Handle tool calls
                    tool_call_deltas = delta.get("tool_calls") or message_payload.get("tool_calls")
                    if tool_call_deltas:
                        for tc_delta in tool_call_deltas:
                            index = tc_delta.get("index", 0)

                            if index not in tool_calls_buffer:
                                tool_calls_buffer[index] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": ""
                                }

                            if tc_delta.get("id"):
                                tool_calls_buffer[index]["id"] = tc_delta["id"]

                            if "function" in tc_delta:
                                func = tc_delta["function"]
                                if func.get("name"):
                                    tool_calls_buffer[index]["name"] = func["name"]
                                if func.get("arguments"):
                                    tool_calls_buffer[index]["arguments"] += func["arguments"]

                    function_call_delta = delta.get("function_call") or message_payload.get("function_call")
                    if function_call_delta:
                        func = function_call_delta or {}
                        index = 0
                        if index not in tool_calls_buffer:
                            tool_calls_buffer[index] = {
                                "id": "",
                                "name": "",
                                "arguments": ""
                            }
                        if func.get("name"):
                            tool_calls_buffer[index]["name"] = func["name"]
                        if func.get("arguments"):
                            tool_calls_buffer[index]["arguments"] += func["arguments"]

                    # Preserve _gemini_raw_parts for Gemini thoughtSignature round-trip
                    # (direct Gemini: list of parts; LinkAI proxy: base64 string of JSON parts)
                    if "_gemini_raw_parts" in delta:
                        gemini_raw_parts = delta["_gemini_raw_parts"]
                    elif isinstance(choice, dict) and choice.get("_gemini_raw_parts"):
                        gemini_raw_parts = choice["_gemini_raw_parts"]

        except AgentCancelledError:
            # Must propagate untouched; never treat as a retryable error.
            raise

        except Exception as e:
            error_str = _private_agent_exception_text_for_classification(e)
            error_str_lower = error_str.lower()
            model_retry_exhausted = '[model_retry_exhausted]' in error_str_lower
            if model_retry_exhausted:
                error_str = error_str.replace("[MODEL_RETRY_EXHAUSTED]", "").strip()
                error_str_lower = error_str.lower()
            
            # Check if error is context overflow (non-retryable, needs session reset)
            # Method 1: Check for special marker (set in stream error handling above)
            is_context_overflow = '[context_overflow]' in error_str_lower
            
            # Method 2: Fallback to keyword matching for non-stream errors
            if not is_context_overflow:
                is_context_overflow = self._is_context_overflow_error(message=error_str)
            context_overflow_is_explicit = (
                ("context_overflow" in error_str_lower and "[context_overflow]" not in error_str_lower)
                or "context_length" in error_str_lower
                or "context length" in error_str_lower
                or "maximum context" in error_str_lower
                or "exceeds model context" in error_str_lower
                or "request_too_large" in error_str_lower
            )
            
            # Check if error is message format error (incomplete tool_use/tool_result pairs)
            # This happens when previous conversation had tool failures or context trimming
            # broke tool_use/tool_result pairs.
            # Note: MiniMax returns error 2013 "tool result's tool id(...) not found" for
            # tool_call_id mismatches — the keywords below are intentionally broad to catch
            # both standard (Claude/OpenAI) and provider-specific (MiniMax) variants.
            is_message_format_error = self._is_message_format_error_text(error_str)
            if is_message_format_error and is_context_overflow and not context_overflow_is_explicit:
                is_context_overflow = False
            
            if is_context_overflow or is_message_format_error:
                error_type = "context overflow" if is_context_overflow else "message format error"
                logger.error(f"💥 {error_type} detected: {_public_agent_exception_message('LLM error redacted.', e)}")

                stream_output_started = bool(full_content or full_reasoning or tool_calls_buffer)
                if is_context_overflow and stream_output_started:
                    logger.warning(
                        "[Agent] Context overflow arrived after model output started; "
                        "suppressing recovery retry to avoid duplicate stream output"
                    )
                    self._emit_event("message_end", {
                        "content": sanitize_assistant_identity(self._filter_think_tags(full_content)),
                        "tool_calls": [],
                        "error": True,
                        "context_overflow_after_output": True,
                        "usage": self.agent.last_usage,
                    })
                    raise Exception(_t(
                        "The model stream reported context overflow after output had started. I stopped instead of retrying to avoid duplicating partial output.",
                        "The model stream reported context overflow after output had started. I stopped instead of retrying to avoid duplicating partial output.",
                    ))

                # Flush memory before trimming to preserve context that will be lost
                if is_context_overflow and self.agent.memory_manager:
                    user_id = getattr(self.agent, '_current_user_id', None)
                    self.agent.memory_manager.flush_memory(
                        messages=self.messages, user_id=user_id,
                        reason="overflow", max_messages=0
                    )

                # Strategy: try aggressive trimming first, only clear as last resort
                if is_context_overflow and not _overflow_retry:
                    recovery = self._aggressive_trim_for_overflow()
                    trim_applied = bool(recovery.get("applied"))
                    imagegen_schema_recovery = (
                        bool(tools_schema)
                        and not trim_applied
                        and "imagegen" in self._tool_schema_intent_groups(self._latest_user_text_for_tool_schema())
                        and "imagegen" in {str(name or "").strip().lower() for name in (self.tools or {})}
                    )
                    schema_only_recovery = bool(tools_schema) and not trim_applied and not imagegen_schema_recovery
                    if trim_applied or schema_only_recovery or imagegen_schema_recovery:
                        force_text_retry = not imagegen_schema_recovery
                        recovery_for_retry = {
                            **recovery,
                            "applied": True,
                            "reason": (
                                "schema_only_imagegen_tool_schema_minimized"
                                if imagegen_schema_recovery else
                                "schema_only_tool_schema_disabled"
                                if schema_only_recovery else recovery.get("reason")
                            ),
                            "trim_applied": trim_applied,
                            "schema_only_recovery": schema_only_recovery,
                            "imagegen_schema_recovery": imagegen_schema_recovery,
                            "tool_schema_disabled": force_text_retry,
                        }
                        retry_schema_budget = {
                            "enabled": not force_text_retry,
                            "reason": "imagegen_context_overflow_recovery" if imagegen_schema_recovery else "forced_text",
                            "force_text_reason": "context_overflow_recovery" if force_text_retry else "",
                            "selected_count": 1 if imagegen_schema_recovery else 0,
                            "deferred_count": max(0, len(self.tools or {}) - (1 if imagegen_schema_recovery else 0)),
                            "selected_tools": ["imagegen"] if imagegen_schema_recovery else [],
                            "deferred_tools": [
                                name
                                for name in sorted((self.tools or {}).keys())
                                if not (imagegen_schema_recovery and str(name or "").strip().lower() == "imagegen")
                            ],
                            "imagegen_intent": imagegen_schema_recovery,
                            "imagegen_available": imagegen_schema_recovery,
                        }
                        retry_budget = self._build_context_budget(
                            self._prepare_messages(),
                            None,
                            retry_schema_budget,
                        )
                        self._emit_event("context_overflow_recovery", {
                            **recovery_for_retry,
                            "retry": True,
                            "force_text_response": force_text_retry,
                            "before_estimated_input_tokens": context_budget.get("estimated_input_tokens"),
                            "before_effective_context_limit_tokens": context_budget.get("effective_context_limit_tokens"),
                            "after_estimated_input_tokens": retry_budget.get("estimated_input_tokens"),
                            "after_effective_context_limit_tokens": retry_budget.get("effective_context_limit_tokens"),
                            "after_remaining_input_tokens": retry_budget.get("remaining_input_tokens"),
                            "after_severity": retry_budget.get("severity"),
                        })
                        logger.warning("🔄 Aggressively trimmed context, retrying...")
                        self._emit_event("message_end", {
                            "content": "",
                            "tool_calls": [],
                            "context_overflow_retry": True,
                            "retrying": True,
                            "usage": self.agent.last_usage,
                        })
                        return self._call_llm_stream(
                            retry_on_empty=retry_on_empty,
                            retry_count=retry_count,
                            max_retries=max_retries,
                            _overflow_retry=True,
                            _force_text_turn=force_text_retry,
                            _force_text_reason="context_overflow_recovery" if force_text_retry else "",
                        )
                    self._emit_event("context_overflow_recovery", {
                        **recovery,
                        "retry": False,
                        "force_text_response": False,
                        "before_estimated_input_tokens": context_budget.get("estimated_input_tokens"),
                        "before_effective_context_limit_tokens": context_budget.get("effective_context_limit_tokens"),
                    })

                # Aggressive trim didn't help or this is a message format error
                # -> clear everything and also purge DB to prevent reload of dirty data
                logger.warning("🔄 Clearing conversation history to recover")
                self.messages.clear()
                self._clear_session_db()
                if is_context_overflow:
                    raise Exception(_t(
                        "抱歉，对话历史过长导致上下文溢出。我已清空历史记录，请重新描述你的需求。",
                        "Sorry, the conversation history got too long and overflowed the context. I've cleared the history — please describe your request again.",
                    ))
                else:
                    raise Exception(_t(
                        "抱歉，之前的对话出现了问题。我已清空历史记录，请重新发送你的消息。",
                        "Sorry, something went wrong with the earlier conversation. I've cleared the history — please send your message again.",
                    ))
            
            # Check if error is rate limit (429)
            is_rate_limit = '429' in error_str_lower or 'rate limit' in error_str_lower
            
            # Check if error is retryable (timeout, connection, server busy, etc.)
            is_retryable = (not model_retry_exhausted) and any(keyword in error_str_lower for keyword in [
                'timeout', 'timed out', 'connection', 'network', 
                'rate limit', 'overloaded', 'unavailable', 'busy', 'retry',
                '429', '500', '502', '503', '504', '512'
            ])
            
            if is_retryable and retry_count < max_retries:
                # Rate limit needs longer wait time
                if is_rate_limit:
                    wait_time = 30 + (retry_count * 15)  # 30s, 45s, 60s for rate limit
                else:
                    wait_time = (retry_count + 1) * 2  # 2s, 4s, 6s for other errors
                
                logger.warning(f"⚠️ LLM API error (attempt {retry_count + 1}/{max_retries}): {_public_agent_exception_message('LLM retryable error redacted.', e)}")
                logger.info(f"Retrying in {wait_time}s...")
                self._sleep_cancelable(wait_time)
                return self._call_llm_stream(
                    retry_on_empty=retry_on_empty, 
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    _overflow_retry=_overflow_retry,
                    _force_text_turn=force_text_response,
                    _force_text_reason=force_text_reason,
                )
            else:
                if retry_count >= max_retries:
                    logger.error(f"❌ LLM API error after {max_retries} retries: {_public_agent_exception_message('LLM error redacted.', e)}")
                else:
                    logger.error(f"❌ LLM call error (non-retryable): {_public_agent_exception_message('LLM error redacted.', e)}")
                if model_retry_exhausted:
                    raise Exception(error_str)
                raise

        # Parse tool calls
        tool_calls = []
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]

            # Ensure tool call has a valid ID (some providers return empty/None IDs)
            tool_id = tc.get("id") or ""
            if not tool_id:
                import uuid
                tool_id = f"call_{uuid.uuid4().hex[:24]}"

            args_str = tc.get("arguments") or ""
            arguments, parse_err = _parse_tool_args(args_str, stop_reason)
            if parse_err:
                logger.error(
                    f"Tool args parse failed for {tc['name']} ({len(args_str)} chars): {parse_err}"
                )
                tool_calls.append({
                    "id": tool_id,
                    "name": self._canonical_tool_name(tc["name"]),
                    "arguments": {},
                    "_parse_error": parse_err,
                })
                continue

            tool_calls.append({
                "id": tool_id,
                "name": self._canonical_tool_name(tc["name"]),
                "arguments": arguments
            })

        # Check for empty response and retry once if enabled
        if retry_on_empty and not full_content and not tool_calls:
            logger.warning(f"⚠️  LLM returned empty response (stop_reason: {stop_reason}), retrying once...")
            self._emit_event("message_end", {
                "content": "",
                "tool_calls": [],
                "empty_retry": True,
                "stop_reason": stop_reason,
                "usage": self.agent.last_usage,
            })
            # Retry without retry flag to avoid infinite loop
            return self._call_llm_stream(
                retry_on_empty=False, 
                retry_count=retry_count,
                max_retries=max_retries,
                _overflow_retry=_overflow_retry,
                _force_text_turn=force_text_response,
                _force_text_reason=force_text_reason,
            )

        # Filter full_content one more time (in case tags were split across chunks)
        full_content = sanitize_assistant_identity(self._filter_think_tags(full_content))
        
        # Add assistant message to history (Claude format uses content blocks)
        assistant_msg = {"role": "assistant", "content": []}

        if full_reasoning:
            stored_reasoning = _truncate_reasoning_for_storage(full_reasoning)
            if len(stored_reasoning) < len(full_reasoning):
                logger.info(
                    f"[reasoning] truncated for storage: "
                    f"{len(full_reasoning)} -> {len(stored_reasoning)} chars"
                )
            assistant_msg["content"].append({
                "type": "thinking",
                "thinking": stored_reasoning
            })

        if full_content:
            assistant_msg["content"].append({
                "type": "text",
                "text": full_content
            })

        # Add tool_use blocks if present
        if tool_calls:
            for tc in tool_calls:
                assistant_msg["content"].append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": tc.get("arguments", {})
                })
        
        if gemini_raw_parts:
            assistant_msg["_gemini_raw_parts"] = gemini_raw_parts

        # Only append if content is not empty
        if assistant_msg["content"]:
            self.messages.append(sanitize_message_identity(assistant_msg))

        self._emit_event("message_end", {
            "content": full_content,
            "tool_calls": tool_calls,
            "usage": self.agent.last_usage,
        })

        return full_content, tool_calls

    def _execute_tool(self, tool_call: Dict) -> Dict[str, Any]:
        """
        Execute tool
        
        Args:
            tool_call: {"id": str, "name": str, "arguments": dict}
            
        Returns:
            Tool execution result
        """
        tool_name = self._canonical_tool_name(tool_call["name"])
        tool_id = tool_call["id"]
        arguments = tool_call["arguments"]
        rerouted_from = ""
        tool_start_emitted = False
        tool_heartbeat_stop: Optional[threading.Event] = None
        tool_heartbeat_thread: Optional[threading.Thread] = None
        task_observer: Optional[TaskObserver] = None

        def emit_tool_start() -> None:
            nonlocal tool_start_emitted
            if tool_start_emitted:
                return
            tool_start_emitted = True
            self._emit_event("tool_execution_start", {
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                "arguments": arguments
            })

        def emit_tool_end(result_payload: Dict[str, Any]) -> None:
            emit_tool_start()
            self._emit_event("tool_execution_end", {
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                **result_payload
            })
            if task_observer is not None:
                task_observer.end(
                    str(result_payload.get("status") or ""),
                    execution_time=result_payload.get("execution_time", 0),
                )

        def start_tool_heartbeat() -> None:
            nonlocal tool_heartbeat_stop, tool_heartbeat_thread, task_observer
            emit_tool_start()
            if tool_heartbeat_thread is not None:
                return
            started_at = time.time()
            policy = _tool_timeout_policy(tool_name, arguments)
            deadline_seconds = float(policy["lease_seconds"])
            max_seconds = float(policy["max_seconds"])
            extension_seconds = float(policy["extension_seconds"])
            extension_count = 0
            task_observer = TaskObserver(
                self._emit_event,
                task_id=f"tool-{tool_id}",
                kind="tool",
                title=tool_name,
                parent_id=tool_id,
                soft_deadline_seconds=int(deadline_seconds),
                hard_deadline_seconds=int(max_seconds),
                metadata={"tool_call_id": tool_id, "tool_name": tool_name, "timeout_reason": policy["reason"]},
                started_at=started_at,
            )
            task_observer.start()
            stop_event = threading.Event()
            tool_heartbeat_stop = stop_event

            def heartbeat_loop() -> None:
                nonlocal deadline_seconds, extension_count
                while not stop_event.wait(TOOL_EXECUTION_HEARTBEAT_SECONDS):
                    elapsed_seconds = round(time.time() - started_at, 2)
                    if elapsed_seconds >= deadline_seconds:
                        if policy["adaptive"] and deadline_seconds < max_seconds:
                            previous_deadline = deadline_seconds
                            deadline_seconds = min(max_seconds, deadline_seconds + extension_seconds)
                            extension_count += 1
                            self._emit_event("tool_execution_deadline_extended", {
                                "tool_call_id": tool_id,
                                "tool_name": tool_name,
                                "elapsed_seconds": elapsed_seconds,
                                "previous_deadline_seconds": int(previous_deadline),
                                "deadline_seconds": int(deadline_seconds),
                                "max_seconds": int(max_seconds),
                                "extension_count": extension_count,
                                "reason": policy["reason"],
                                "status": "running",
                            })
                            if task_observer is not None:
                                task_observer.extended(
                                    elapsed_seconds=elapsed_seconds,
                                    previous_deadline_seconds=int(previous_deadline),
                                    deadline_seconds=int(deadline_seconds),
                                    max_seconds=int(max_seconds),
                                    reason=policy["reason"],
                                )
                            self._emit_event("tool_execution_heartbeat", {
                                "tool_call_id": tool_id,
                                "tool_name": tool_name,
                                "elapsed_seconds": elapsed_seconds,
                                "deadline_seconds": int(deadline_seconds),
                                "max_seconds": int(max_seconds),
                                "extension_count": extension_count,
                                "status": "running",
                            })
                            if task_observer is not None:
                                task_observer.heartbeat(
                                    elapsed_seconds=elapsed_seconds,
                                    deadline_seconds=int(deadline_seconds),
                                    max_seconds=int(max_seconds),
                                    extension_count=extension_count,
                                )
                            continue
                        logger.warning(
                            "[Agent] tool execution timeout: tool=%s id=%s elapsed=%ss deadline=%ss max=%ss adaptive=%s",
                            tool_name,
                            tool_id,
                            elapsed_seconds,
                            deadline_seconds,
                            max_seconds,
                            policy["adaptive"],
                        )
                        self._emit_event("tool_execution_timeout", {
                            "tool_call_id": tool_id,
                            "tool_name": tool_name,
                            "elapsed_seconds": elapsed_seconds,
                            "timeout_seconds": int(deadline_seconds),
                            "max_seconds": int(max_seconds),
                            "extension_count": extension_count,
                            "status": "timeout",
                            "error_code": "TOOL_TIMEOUT",
                            "message": (
                                f"Tool '{tool_name}' exceeded the {int(deadline_seconds)}s execution deadline. "
                                "The run was marked timed out to avoid an indefinitely active session. "
                                "For legitimately longer work, retry with a larger tool timeout or split the task."
                            ),
                        })
                        if task_observer is not None:
                            task_observer.intervention_requested(
                                elapsed_seconds=elapsed_seconds,
                                timeout_seconds=int(deadline_seconds),
                                max_seconds=int(max_seconds),
                                extension_count=extension_count,
                                reason="tool_timeout",
                                next_actions=["continue", "stop", "background"],
                            )
                            task_observer.timeout(
                                elapsed_seconds=elapsed_seconds,
                                timeout_seconds=int(deadline_seconds),
                                max_seconds=int(max_seconds),
                                extension_count=extension_count,
                                reason="tool_timeout",
                            )
                        if self.cancel_event is not None:
                            self.cancel_event.set()
                        break
                    self._emit_event("tool_execution_heartbeat", {
                        "tool_call_id": tool_id,
                        "tool_name": tool_name,
                        "elapsed_seconds": elapsed_seconds,
                        "deadline_seconds": int(deadline_seconds),
                        "max_seconds": int(max_seconds),
                        "extension_count": extension_count,
                        "status": "running",
                    })
                    if task_observer is not None:
                        task_observer.heartbeat(
                            elapsed_seconds=elapsed_seconds,
                            deadline_seconds=int(deadline_seconds),
                            max_seconds=int(max_seconds),
                            extension_count=extension_count,
                        )

            tool_heartbeat_thread = threading.Thread(
                target=heartbeat_loop,
                name=f"ecorex-tool-heartbeat-{str(tool_id)[:8]}",
                daemon=True,
            )
            tool_heartbeat_thread.start()

        def stop_tool_heartbeat() -> None:
            if tool_heartbeat_stop is not None:
                tool_heartbeat_stop.set()

        if "_parse_error" in tool_call:
            result = {
                "status": "error",
                "result": tool_call["_parse_error"],
                "execution_time": 0,
            }
            self._record_tool_result(tool_name, arguments, False)
            emit_tool_end(result)
            return result

        # Check for consecutive failures (retry protection)
        should_stop, stop_reason, is_critical = self._check_consecutive_failures(tool_name, arguments)
        if should_stop:
            logger.error(f"🛑 {stop_reason}")
            self._record_tool_result(tool_name, arguments, False)
            
            if is_critical:
                # Critical failure - abort entire conversation
                result = {
                    "status": "critical_error",
                    "result": stop_reason,
                    "execution_time": 0
                }
            else:
                # Normal failure - let LLM try different approach
                self._force_text_response_once("consecutive-tool-failure-budget")
                result = {
                    "status": "error",
                    "result": f"{stop_reason}\n\n当前方法行不通，请尝试完全不同的方法或向用户询问更多信息。",
                    "execution_time": 0
                }
            emit_tool_end(result)
            return result

        autoroute_name, autoroute_args, autoroute_reason = self._external_capability_autoroute(tool_name, arguments)
        if autoroute_name:
            logger.info(
                f"[Agent] Auto-routing external capability from {tool_name} "
                f"to {autoroute_name}: {autoroute_reason}"
            )
            rerouted_from = f"{tool_name}:{autoroute_reason}"
            tool_name = autoroute_name
            arguments = autoroute_args

        reroute_reason = self._external_capability_reroute(tool_name, arguments)
        if reroute_reason:
            logger.warning(f"[Agent] External capability rerouted for {tool_name}: {reroute_reason}")
            self._record_tool_result(tool_name, arguments, False)
            result = {
                "status": "error",
                "result": reroute_reason,
                "execution_time": 0,
            }
            emit_tool_end(result)
            return result

        chain_stop, chain_reason = self._check_tool_chain_budget(tool_name, arguments)
        if chain_stop:
            logger.warning(f"[Agent] Tool-chain budget stop for {tool_name}: {chain_reason}")
            self._record_tool_result(tool_name, arguments, False)
            self._force_text_response_once("external-capability-chain-budget")
            result = {
                "status": "error",
                "result": "已停止重复调用同一能力，正在整理已获得的信息。",
                "execution_time": 0,
            }
            emit_tool_end(result)
            return result

        try:
            tool = self.tools.get(tool_name)
            if not tool:
                raise ValueError(self._build_tool_not_found_message(tool_name))
        except Exception as e:
            logger.error(f"Tool lookup error: {_public_agent_exception_message('Tool lookup failed.', e)}")
            error_result = _public_agent_tool_error_result("Tool lookup failed.", e)
            self._record_tool_result(tool_name, arguments, False)
            emit_tool_end(error_result)
            return error_result

        permission_tool_name, permission_arguments = self._permission_proxy_for_tool(tool, tool_name, arguments)
        permission = self._authorize_tool_execution(permission_tool_name, tool_id, permission_arguments)
        if permission.get("allowed") is not True:
            if permission.get("cancelled"):
                raise AgentCancelledError(
                    permission.get("reason") or "agent cancelled while waiting for tool permission"
                )
            reason = permission.get("reason") or "User denied local tool execution."
            self._force_text_response_once("permission-denied")
            result = {
                "status": "error",
                "result": (
                    f"{reason}\n\n"
                    "Permission blocked this external capability. Do not retry the same tool now; "
                    "summarize the blocker and ask the user to change the access mode, approve the request, "
                    "or provide the missing authorization."
                ),
                "execution_time": 0
            }
            self._record_tool_result(tool_name, arguments, False)
            emit_tool_end(result)
            return result

        start_tool_heartbeat()

        try:
            # Set tool context
            tool.model = self.model
            tool.context = self.agent
            tool.cancel_event = self.cancel_event
            tool.emit_event = self._emit_event
            tool.tool_call_id = tool_id
            if tool_name == "imagegen":
                tool.artifact_naming_context = self._image_artifact_naming_context()

            # Execute tool
            start_time = time.time()
            result: ToolResult = tool.execute_tool(arguments)
            execution_time = time.time() - start_time
            result_payload = result.result
            if rerouted_from:
                if isinstance(result_payload, dict):
                    result_payload = {
                        **result_payload,
                        "reroutedFrom": rerouted_from,
                    }
                else:
                    result_payload = {
                        "output": result_payload,
                        "reroutedFrom": rerouted_from,
                    }

            result_dict = {
                "status": result.status,
                "result": result_payload,
                "execution_time": execution_time
            }

            blocker = self._tool_result_user_action_blocker(tool_name, result_payload)
            if blocker:
                self._force_text_response_once(blocker)

            if tool_name == "host_diagnostics":
                action = str(arguments.get("action") or "status").strip().lower()
                if result.status == "success" and action in {"logs", "all"}:
                    self._force_text_response_once("host-diagnostics-logs-ready")

            # Record tool result for failure tracking
            success = result.status == "success"
            self._record_tool_result(tool_name, arguments, success)

            # Auto-refresh skills after skill creation
            if tool_name == "bash" and result.status == "success":
                command = arguments.get("command", "")
                if "init_skill.py" in command and self.agent.skill_manager:
                    logger.info("Detected skill creation, refreshing skills...")
                    self.agent.refresh_skills()
                    logger.info(f"Skills refreshed! Now have {len(self.agent.skill_manager.skills)} skills")

            emit_tool_end(result_dict)

            return result_dict

        except Exception as e:
            logger.error(f"Tool execution error: {_public_agent_exception_message('Tool execution failed.', e)}")
            error_result = _public_agent_tool_error_result("Tool execution failed.", e)
            # Record failure
            self._record_tool_result(tool_name, arguments, False)
            
            emit_tool_end(error_result)
            return error_result
        finally:
            stop_tool_heartbeat()

    def _build_tool_not_found_message(self, tool_name: str) -> str:
        """Build a helpful error message when a tool is not found.

        If a skill with the same name exists in skill_manager, read its
        SKILL.md and include the content so the LLM knows how to use it.
        """
        available_tools = list(self.tools.keys())
        base_msg = f"Tool '{tool_name}' not found. Available tools: {available_tools}"

        skill_manager = getattr(self.agent, 'skill_manager', None)
        if not skill_manager:
            return base_msg

        skill_entry = skill_manager.get_skill(tool_name)
        if not skill_entry:
            return base_msg

        skill = skill_entry.skill
        skill_md_path = skill.file_path
        skill_content = ""
        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                skill_content = f.read()
        except Exception:
            skill_content = skill.description

        logger.info(
            f"[Agent] Tool '{tool_name}' not found, but matched skill '{skill.name}'. "
            f"Guiding LLM to use the skill instead."
        )

        return (
            f"Tool '{tool_name}' is not a built-in tool, but a matching skill "
            f"'{skill.name}' is available. Read and follow the skill instructions below, "
            f"then choose the most specific available tool for each step. Do not fall back "
            f"to raw shell probing when a dedicated host tool such as `feishu_cli`, "
            f"`host_diagnostics`, or the configured browser/CDP path applies:\n\n"
            f"--- SKILL: {skill.name} (path: {skill_md_path}) ---\n"
            f"{skill_content}\n"
            f"--- END SKILL ---\n\n"
            f"Available tools: {available_tools}"
        )

    def _validate_and_fix_messages(self):
        """Delegate to the shared sanitizer (see message_sanitizer.py)."""
        sanitize_claude_messages(self.messages)

    def _identify_complete_turns(self) -> List[Dict]:
        """
        识别完整的对话轮次
        
        一个完整轮次包括：
        1. 用户消息（text）
        2. AI 回复（可能包含 tool_use）
        3. 工具结果（tool_result，如果有）
        4. 后续 AI 回复（如果有）
        
        Returns:
            List of turns, each turn is a dict with 'messages' list
        """
        turns = []
        current_turn = {'messages': []}
        
        for msg in self.messages:
            role = msg.get('role')
            content = msg.get('content', [])
            
            if role == 'user':
                # Determine if this is a real user query (not a tool_result injection
                # or an internal hint message injected by the agent loop).
                is_user_query = False
                has_tool_result = False
                if isinstance(content, list):
                    has_text = any(
                        isinstance(block, dict) and block.get('type') == 'text'
                        for block in content
                    )
                    has_tool_result = any(
                        isinstance(block, dict) and block.get('type') == 'tool_result'
                        for block in content
                    )
                    # A message with tool_result is always internal, even if it
                    # also contains text blocks (shouldn't happen, but be safe).
                    is_user_query = has_text and not has_tool_result
                elif isinstance(content, str):
                    is_user_query = True
                
                if is_user_query:
                    if current_turn['messages']:
                        turns.append(current_turn)
                    current_turn = {'messages': [msg]}
                else:
                    current_turn['messages'].append(msg)
            else:
                # AI 回复，属于当前轮次
                current_turn['messages'].append(msg)
        
        # 添加最后一个轮次
        if current_turn['messages']:
            turns.append(current_turn)
        
        return turns
    
    def _estimate_turn_tokens(self, turn: Dict) -> int:
        """估算一个轮次的 tokens"""
        return sum(
            self.agent._estimate_message_tokens(msg) 
            for msg in turn['messages']
        )

    def _truncate_historical_tool_results(self):
        """
        Truncate tool_result content in historical messages to reduce context size.

        Current turn results are kept at 30K chars (truncated at creation time).
        Historical turn results are further truncated to 10K chars here.
        This runs before token-based trimming so that we first shrink oversized
        results, potentially avoiding the need to drop entire turns.
        """
        MAX_HISTORY_RESULT_CHARS = 20000

        if len(self.messages) < 2:
            return

        # Find where the last user text message starts (= current turn boundary)
        # We skip the current turn's messages to preserve their full content
        current_turn_start = len(self.messages)
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text" for b in content
                ):
                    current_turn_start = i
                    break
                elif isinstance(content, str):
                    current_turn_start = i
                    break

        truncated_count = 0
        for i in range(current_turn_start):
            msg = self.messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result_str = block.get("content", "")
                if isinstance(result_str, str) and len(result_str) > MAX_HISTORY_RESULT_CHARS:
                    original_len = len(result_str)
                    block["content"] = result_str[:MAX_HISTORY_RESULT_CHARS] + \
                        f"\n\n[Historical output truncated: {original_len} -> {MAX_HISTORY_RESULT_CHARS} chars]"
                    truncated_count += 1

        if truncated_count > 0:
            logger.info(f"📎 Truncated {truncated_count} historical tool result(s) to {MAX_HISTORY_RESULT_CHARS} chars")

    def _context_overflow_recovery_payload(
        self,
        *,
        original_count: int,
        turns_before: List[Dict[str, Any]],
        removed_turns: int,
        truncated_blocks: int,
        truncated_current_run_blocks: int,
        truncated_historical_blocks: int,
        truncated_historical_user_messages: int,
        current_user_marker: str,
    ) -> Dict[str, Any]:
        turns_after = self._identify_complete_turns()
        serialized_after = json.dumps(self.messages, ensure_ascii=False)
        current_turn_preserved = bool(
            turns_after
            and (
                not current_user_marker
                or current_user_marker in serialized_after
            )
        )
        applied = bool(removed_turns or truncated_blocks)
        return {
            "applied": applied,
            "reason": "trimmed" if applied else "nothing_to_trim",
            "messages_before": original_count,
            "messages_after": len(self.messages),
            "turns_before": len(turns_before),
            "turns_after": len(turns_after),
            "removed_turns": removed_turns,
            "truncated_blocks": truncated_blocks,
            "truncated_current_run_blocks": truncated_current_run_blocks,
            "truncated_historical_blocks": truncated_historical_blocks,
            "truncated_historical_user_messages": truncated_historical_user_messages,
            "current_turn_preserved": current_turn_preserved,
        }

    def _aggressive_trim_for_overflow(self) -> Dict[str, Any]:
        """
        Aggressively trim context when a real overflow error is returned by the API.

        This method goes beyond normal _trim_messages by:
        1. Truncating all tool results (including current turn) to a small limit
        2. Keeping only the last 5 complete conversation turns
        3. Truncating overly long user messages

        Returns:
            Structured recovery metadata. ``applied`` indicates whether a retry
            is worth attempting.
        """
        if not self.messages:
            return {
                "applied": False,
                "reason": "no_messages",
                "messages_before": 0,
                "messages_after": 0,
                "turns_before": 0,
                "turns_after": 0,
                "removed_turns": 0,
                "truncated_blocks": 0,
                "current_turn_preserved": False,
            }

        original_count = len(self.messages)
        turns_before = self._identify_complete_turns()
        current_turn_message_ids = (
            {id(msg) for msg in turns_before[-1]["messages"]}
            if turns_before else set()
        )
        current_user_marker = self._latest_user_text_for_tool_schema()[:200]

        # Step 1: Aggressively truncate ALL tool results to 10K chars
        AGGRESSIVE_LIMIT = 10000
        truncated = 0
        truncated_current_blocks = 0
        truncated_historical_blocks = 0
        truncated_historical_user_messages = 0

        def _mark_truncated(msg: Dict[str, Any]) -> None:
            nonlocal truncated, truncated_current_blocks, truncated_historical_blocks
            truncated += 1
            if id(msg) in current_turn_message_ids:
                truncated_current_blocks += 1
            else:
                truncated_historical_blocks += 1

        for msg in self.messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                # Truncate tool_result blocks
                if block.get("type") == "tool_result":
                    result_str = block.get("content", "")
                    if isinstance(result_str, str) and len(result_str) > AGGRESSIVE_LIMIT:
                        block["content"] = (
                            result_str[:AGGRESSIVE_LIMIT]
                            + f"\n\n[Truncated for context recovery: "
                            f"{len(result_str)} -> {AGGRESSIVE_LIMIT} chars]"
                        )
                        _mark_truncated(msg)
                # Truncate tool_use input blocks (e.g. large write content)
                if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    input_str = json.dumps(block["input"], ensure_ascii=False)
                    if len(input_str) > AGGRESSIVE_LIMIT:
                        # Keep only a summary of the input
                        input_truncated = False
                        for key, val in block["input"].items():
                            if isinstance(val, str) and len(val) > 1000:
                                block["input"][key] = (
                                    val[:1000]
                                    + f"... [truncated {len(val)} chars]"
                                )
                                input_truncated = True
                        if input_truncated:
                            _mark_truncated(msg)

        # Step 2: Truncate overly long user text messages (e.g. pasted content)
        USER_MSG_LIMIT = 10000
        for msg in self.messages:
            if msg.get("role") != "user" or id(msg) in current_turn_message_ids:
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if len(text) > USER_MSG_LIMIT:
                            block["text"] = (
                                text[:USER_MSG_LIMIT]
                                + f"\n\n[Message truncated for context recovery: "
                                f"{len(text)} -> {USER_MSG_LIMIT} chars]"
                            )
                            truncated_historical_user_messages += 1
                            _mark_truncated(msg)
            elif isinstance(content, str) and len(content) > USER_MSG_LIMIT:
                msg["content"] = (
                    content[:USER_MSG_LIMIT]
                    + f"\n\n[Message truncated for context recovery: "
                    f"{len(content)} -> {USER_MSG_LIMIT} chars]"
                )
                truncated_historical_user_messages += 1
                _mark_truncated(msg)

        # Step 3: Keep only the last 5 complete turns
        turns = self._identify_complete_turns()
        removed = 0
        if len(turns) > 5:
            kept_turns = turns[-5:-1] + turns[-1:]
            new_messages = []
            for turn in kept_turns:
                new_messages.extend(turn["messages"])
            removed = len(turns) - len(kept_turns)
            self.messages[:] = new_messages
            logger.info(
                f"🔧 Aggressive trim: removed {removed} old turns, "
                f"truncated {truncated} large blocks, "
                f"{original_count} -> {len(self.messages)} messages"
            )
            return self._context_overflow_recovery_payload(
                original_count=original_count,
                turns_before=turns_before,
                removed_turns=removed,
                truncated_blocks=truncated,
                truncated_current_run_blocks=truncated_current_blocks,
                truncated_historical_blocks=truncated_historical_blocks,
                truncated_historical_user_messages=truncated_historical_user_messages,
                current_user_marker=current_user_marker,
            )

        if truncated > 0:
            logger.info(
                f"🔧 Aggressive trim: truncated {truncated} large blocks "
                f"(no turns removed, only {len(turns)} turn(s) left)"
            )
            return self._context_overflow_recovery_payload(
                original_count=original_count,
                turns_before=turns_before,
                removed_turns=removed,
                truncated_blocks=truncated,
                truncated_current_run_blocks=truncated_current_blocks,
                truncated_historical_blocks=truncated_historical_blocks,
                truncated_historical_user_messages=truncated_historical_user_messages,
                current_user_marker=current_user_marker,
            )

        # Nothing left to trim
        logger.warning("🔧 Aggressive trim: nothing to trim, will clear history")
        return self._context_overflow_recovery_payload(
            original_count=original_count,
            turns_before=turns_before,
            removed_turns=removed,
            truncated_blocks=truncated,
            truncated_current_run_blocks=truncated_current_blocks,
            truncated_historical_blocks=truncated_historical_blocks,
            truncated_historical_user_messages=truncated_historical_user_messages,
            current_user_marker=current_user_marker,
        )

    def _build_context_summary_callback(self, discarded_turns: list, kept_turns: list):
        """
        Build a callback that injects an LLM summary into the first user
        message of *kept_turns*. Returns None if no valid injection target.

        The callback is passed to flush_from_messages so that the same LLM
        call that writes daily memory also provides the in-context summary.
        """
        if not kept_turns:
            return None

        # Find the first user text block in kept_turns as injection target
        target_block = None
        for turn in kept_turns:
            for msg in turn["messages"]:
                if msg.get("role") == "user":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                target_block = block
                                break
                    if target_block:
                        break
            if target_block:
                break

        if not target_block:
            return None

        turn_count = len(discarded_turns)
        original_text = target_block["text"]

        def _on_summary_ready(summary: str):
            if not summary or not summary.strip():
                return
            target_block["text"] = (
                f"[System: Previous conversation summary — "
                f"{turn_count} turns were compacted]\n\n"
                f"{summary.strip()}\n\n"
                f"The recent conversation continues below.\n\n---\n\n"
                f"{original_text}"
            )
            logger.info(
                f"📝 Context summary injected "
                f"({len(summary)} chars, {turn_count} turns)"
            )

        return _on_summary_ready

    def _trim_messages(self):
        """
        智能清理消息历史，保持对话完整性

        使用完整轮次作为清理单位，确保：
        1. 不会在对话中间截断
        2. 工具调用链（tool_use + tool_result）保持完整
        3. 每轮对话都是完整的（用户消息 + AI回复 + 工具调用）
        """
        if not self.messages or not self.agent:
            return

        # Step 0: Truncate large tool results in historical turns (30K -> 10K)
        self._truncate_historical_tool_results()

        # Step 1: 识别完整轮次
        turns = self._identify_complete_turns()
        
        if not turns:
            return
        
        # Step 2: 轮次限制 - 超出时移除前一半，保留后一半
        if len(turns) > self.max_context_turns:
            removed_count = len(turns) // 2
            keep_count = len(turns) - removed_count
            
            discarded_turns = turns[:removed_count]
            turns = turns[-keep_count:]

            logger.info(
                f"💾 Context turns exceeded: {keep_count + removed_count} > {self.max_context_turns}, "
                f"trimmed to {keep_count} turns (removed {removed_count})"
            )

            # Flush to daily memory + inject context summary (single async LLM call)
            if self.agent.memory_manager:
                discarded_messages = []
                for turn in discarded_turns:
                    discarded_messages.extend(turn["messages"])
                if discarded_messages:
                    user_id = getattr(self.agent, '_current_user_id', None)
                    cb = self._build_context_summary_callback(discarded_turns, turns)
                    self.agent.memory_manager.flush_memory(
                        messages=discarded_messages, user_id=user_id,
                        reason="trim", max_messages=0,
                        context_summary_callback=cb,
                    )

        # Step 3: Token 限制 - 保留完整轮次
        # Use the same effective limit reported by context_budget. This clamps
        # oversized configured limits to the model window minus response reserve.
        budget_limits = self._context_budget_limits()
        max_tokens = budget_limits["effective_context_limit_tokens"]

        # Estimate system prompt tokens
        system_tokens = self.agent._estimate_message_tokens({"role": "system", "content": self.system_prompt})

        # Calculate current tokens
        current_tokens = sum(self._estimate_turn_tokens(turn) for turn in turns)
        
        # If under limit, reconstruct messages and return
        if current_tokens + system_tokens <= max_tokens:
            # Reconstruct message list from turns
            new_messages = []
            for turn in turns:
                new_messages.extend(turn['messages'])
            
            old_count = len(self.messages)
            self.messages = new_messages
            
            # Log if we removed messages due to turn limit
            if old_count > len(self.messages):
                logger.info(f"   Rebuilt message list: {old_count} -> {len(self.messages)} messages")
            return

        # Token limit exceeded — tiered strategy based on turn count:
        #
        #   Few turns (<5):  Compress ALL turns to text-only (strip tool chains,
        #                    keep user query + final reply).  Never discard turns
        #                    — losing even one is too painful when context is thin.
        #
        #   Many turns (>=5): Directly discard the first half of turns.
        #                     With enough turns the oldest ones are less
        #                     critical, and keeping the recent half intact
        #                     (with full tool chains) is more useful.

        COMPRESS_THRESHOLD = 5

        if len(turns) < COMPRESS_THRESHOLD:
            # --- Few turns: compress ALL turns to text-only, never discard ---
            compressed_turns = []
            for t in turns:
                compressed = compress_turn_to_text_only(t)
                if compressed["messages"]:
                    compressed_turns.append(compressed)

            new_messages = []
            for turn in compressed_turns:
                new_messages.extend(turn["messages"])

            new_tokens = sum(self._estimate_turn_tokens(t) for t in compressed_turns)
            old_count = len(self.messages)
            self.messages = new_messages

            logger.info(
                f"📦 Context tokens exceeded (turns<{COMPRESS_THRESHOLD}): "
                f"~{current_tokens + system_tokens} > {max_tokens}, "
                f"compressed all {len(turns)} turns to plain text "
                f"({old_count} -> {len(self.messages)} messages, "
                f"~{current_tokens + system_tokens} -> ~{new_tokens + system_tokens} tokens)"
            )
            return

        # --- Many turns (>=5): discard the older half, keep the newer half ---
        removed_count = len(turns) // 2
        keep_count = len(turns) - removed_count
        discarded_turns = turns[:removed_count]
        kept_turns = turns[-keep_count:]
        kept_tokens = sum(self._estimate_turn_tokens(t) for t in kept_turns)

        logger.info(
            f"🔄 Context tokens exceeded: ~{current_tokens + system_tokens} > {max_tokens}, "
            f"trimmed to {keep_count} turns (removed {removed_count})"
        )

        if self.agent.memory_manager:
            discarded_messages = []
            for turn in discarded_turns:
                discarded_messages.extend(turn["messages"])
            if discarded_messages:
                user_id = getattr(self.agent, '_current_user_id', None)
                cb = self._build_context_summary_callback(discarded_turns, kept_turns)
                self.agent.memory_manager.flush_memory(
                    messages=discarded_messages, user_id=user_id,
                    reason="trim", max_messages=0,
                    context_summary_callback=cb,
                )

        new_messages = []
        for turn in kept_turns:
            new_messages.extend(turn['messages'])

        old_count = len(self.messages)
        self.messages = new_messages

        logger.info(
            f"   Removed {removed_count} turns "
            f"({old_count} -> {len(self.messages)} messages, "
            f"~{current_tokens + system_tokens} -> ~{kept_tokens + system_tokens} tokens)"
        )

    def _clear_session_db(self):
        """
        Clear the current session's persisted messages from SQLite DB.

        This prevents dirty data (broken tool_use/tool_result pairs) from being
        reloaded on the next request or after a restart.
        """
        try:
            session_id = getattr(self.agent, '_current_session_id', None)
            if not session_id:
                return
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            store.clear_session(session_id)
            try:
                from models.openai.responses_state_store import clear_responses_state_for_session

                removed = clear_responses_state_for_session(session_id)
                if removed:
                    logger.info(f"Cleared Responses state for dirty session: {session_id}, removed={removed}")
            except Exception as e:
                logger.warning(f"Failed to clear Responses state for dirty session {session_id}: {_public_agent_exception_message('Responses state cleanup failed.', e)}")
            logger.info(f"🗑️ Cleared dirty session data from DB: {session_id}")
        except Exception as e:
            logger.warning(f"Failed to clear session DB: {_public_agent_exception_message('Session DB cleanup failed.', e)}")

    def _prepare_messages(self) -> List[Dict[str, Any]]:
        """
        Prepare messages to send to LLM
        
        Note: For Claude API, system prompt should be passed separately via system parameter,
        not as a message. The AgentLLMModel will handle this.
        """
        # Don't add system message here - it will be handled separately by the LLM adapter
        return self.messages

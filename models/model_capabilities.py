# encoding:utf-8
"""Central model capability helpers for OpenAI-compatible chat calls.

The runtime supports many providers through OpenAI-shaped APIs, but not every
model accepts the same request parameters. Keep the shared rules here so agent
calls, model settings, and tests do not each grow their own model-name checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from common import const


SAMPLING_KEYS: Tuple[str, ...] = (
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
)


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_penalties: bool = True
    supports_tools: bool = True
    supports_stream: bool = True
    supports_stream_usage: bool = False
    supports_system_messages: bool = True
    supports_reasoning_effort: bool = False
    supports_verbosity: bool = False
    supports_thinking_param: bool = False
    reasoning_effort_values: Tuple[str, ...] = ()
    reasoning_style: str = "none"
    max_tokens_param: str = "max_tokens"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_FIXED_SAMPLING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_FIXED_SAMPLING_EXACT = {
    const.GPT_5,
    const.GPT_5_MINI,
    const.GPT_5_NANO,
    const.GPT_54,
    const.GPT_54_MINI,
    const.GPT_54_NANO,
    const.GPT_55,
    const.O1,
    const.O1_MINI,
    "o1",
    "o3",
    "o3-mini",
    "o4-mini",
}

_PROVIDER_EXACT = {
    "wenxin": const.BAIDU,
    "wenxin-4": const.BAIDU,
    const.QIANFAN: const.QIANFAN,
    const.MODELSCOPE: const.MODELSCOPE,
    const.QWEN_TURBO: const.QWEN_DASHSCOPE,
    const.QWEN_PLUS: const.QWEN_DASHSCOPE,
    const.QWEN_MAX: const.QWEN_DASHSCOPE,
    const.MOONSHOT: const.MOONSHOT,
    "moonshot-v1-8k": const.MOONSHOT,
    "moonshot-v1-32k": const.MOONSHOT,
    "moonshot-v1-128k": const.MOONSHOT,
    "abab6.5-chat": const.MiniMax,
}

_PROVIDER_PREFIXES = (
    ("qwen/", const.MODELSCOPE),
    ("deepseek-ai/", const.MODELSCOPE),
    ("llm-research/", const.MODELSCOPE),
    ("moonshotai/", const.MODELSCOPE),
    ("meituan-longcat/", const.MODELSCOPE),
    ("xiaomimimo/", const.MODELSCOPE),
    ("minimax/", const.MODELSCOPE),
    ("zhipuai/", const.MODELSCOPE),
    ("opencompass/", const.MODELSCOPE),
    ("opengvlab/", const.MODELSCOPE),
    ("mistralai/", const.MODELSCOPE),
    ("stepfun-ai/", const.MODELSCOPE),
    ("shanghai_ai_laboratory/", const.MODELSCOPE),
    ("musepublic/", const.MODELSCOPE),
    ("qwen", const.QWEN_DASHSCOPE),
    ("qwq", const.QWEN_DASHSCOPE),
    ("qvq", const.QWEN_DASHSCOPE),
    ("gemini", const.GEMINI),
    ("glm", const.ZHIPU_AI),
    ("claude", const.CLAUDEAPI),
    ("moonshot", const.MOONSHOT),
    ("kimi", const.MOONSHOT),
    ("doubao", const.DOUBAO),
    ("deepseek", const.DEEPSEEK),
    ("ernie", const.QIANFAN),
    ("mimo-", const.MIMO),
    ("minimax", const.MiniMax),
    ("linkai-", const.LINKAI),
)

_OFFICIAL_OPENAI_CHAT_PROVIDERS = {
    const.OPENAI,
    const.OPEN_AI,
    const.CHATGPT,
    const.CHATGPTONAZURE,
    "openai",
}

_OPENAI_BASE_SENSITIVE_PROVIDERS = {
    const.OPENAI,
    const.OPEN_AI,
    const.CHATGPT,
    "openai",
}

_OPENAI_REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high")
_DEEPSEEK_REASONING_EFFORT_VALUES = ("high", "max")


def normalize_model_name(model: Optional[str]) -> str:
    return str(model or "").strip()


def infer_provider_id(
    model: Optional[str],
    *,
    configured_bot_type: Optional[str] = "",
    use_linkai: bool = False,
    has_linkai_key: bool = False,
) -> str:
    """Infer the provider id used by the local runtime for a chat model."""
    if use_linkai and has_linkai_key:
        return const.LINKAI
    if configured_bot_type:
        return const.OPENAI if configured_bot_type == const.CHATGPT else str(configured_bot_type)

    model_name = normalize_model_name(model)
    if not model_name:
        return const.OPENAI
    if model_name in getattr(const, "MODELSCOPE_MODEL_LIST", ()):
        return const.MODELSCOPE
    if model_name in _PROVIDER_EXACT:
        return _PROVIDER_EXACT[model_name]
    lowered = model_name.lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return provider
    return const.OPENAI


def _fixed_sampling_model(model_name: str) -> bool:
    lowered = model_name.lower()
    return lowered in _FIXED_SAMPLING_EXACT or lowered.startswith(_FIXED_SAMPLING_PREFIXES)


def get_model_capabilities(model: Optional[str], provider: Optional[str] = None) -> ModelCapabilities:
    model_name = normalize_model_name(model)
    provider_id = provider or infer_provider_id(model_name)
    supports_temperature = True
    supports_top_p = True
    supports_penalties = True
    supports_system_messages = True
    supports_reasoning_effort = False
    supports_verbosity = False
    supports_thinking_param = False
    reasoning_effort_values: Tuple[str, ...] = ()
    reasoning_style = "none"
    max_tokens_param = "max_tokens"
    official_openai_chat = provider_id in _OFFICIAL_OPENAI_CHAT_PROVIDERS
    lowered_model = model_name.lower()

    if official_openai_chat and _fixed_sampling_model(model_name):
        supports_temperature = False
        supports_top_p = False
        supports_penalties = False
        supports_reasoning_effort = True
        supports_verbosity = True
        reasoning_effort_values = _OPENAI_REASONING_EFFORT_VALUES
        reasoning_style = "reasoning_effort"
        max_tokens_param = "max_completion_tokens"
    if lowered_model.startswith("o1"):
        supports_system_messages = False

    if provider_id == const.DEEPSEEK and lowered_model.startswith("deepseek-v4"):
        supports_temperature = False
        supports_top_p = False
        supports_penalties = False
        supports_reasoning_effort = True
        supports_thinking_param = True
        reasoning_effort_values = _DEEPSEEK_REASONING_EFFORT_VALUES
        reasoning_style = "thinking"
    elif provider_id == const.QWEN_DASHSCOPE and (
        lowered_model.startswith("qwen3")
        or lowered_model.startswith("qwq")
    ):
        supports_thinking_param = True
        reasoning_style = "thinking"
    elif provider_id == const.ZHIPU_AI and (
        lowered_model.startswith("glm-4.7")
        or lowered_model.startswith("glm-5")
    ):
        supports_thinking_param = True
        reasoning_style = "thinking"
    elif provider_id == const.MOONSHOT and (
        lowered_model.startswith("kimi-k2")
        or lowered_model.startswith("kimi-k1.5")
    ):
        supports_thinking_param = True
        reasoning_style = "thinking"
    elif provider_id == const.MIMO and lowered_model.startswith("mimo-"):
        supports_thinking_param = True
        reasoning_style = "thinking"
        max_tokens_param = "max_completion_tokens"
    elif provider_id == const.DOUBAO and lowered_model.startswith("doubao-seed-"):
        supports_temperature = False
        supports_thinking_param = True
        reasoning_style = "thinking"

    supports_stream_usage = official_openai_chat
    return ModelCapabilities(
        provider=provider_id,
        model=model_name,
        supports_temperature=supports_temperature,
        supports_top_p=supports_top_p,
        supports_penalties=supports_penalties,
        supports_stream_usage=supports_stream_usage,
        supports_system_messages=supports_system_messages,
        supports_reasoning_effort=supports_reasoning_effort,
        supports_verbosity=supports_verbosity,
        supports_thinking_param=supports_thinking_param,
        reasoning_effort_values=reasoning_effort_values,
        reasoning_style=reasoning_style,
        max_tokens_param=max_tokens_param,
    )


def resolve_capability_provider_for_base(provider: str, api_base: Optional[str]) -> str:
    """Downgrade OpenAI aliases to generic compatible when the host is not official."""
    provider_id = provider or ""
    if provider_id not in _OPENAI_BASE_SENSITIVE_PROVIDERS:
        return provider_id
    if not api_base:
        return const.OPENAI
    from models.openai.responses_adapter import is_official_openai_provider

    if is_official_openai_provider(provider_id, api_base):
        return const.OPENAI
    return "openai_compatible"


def capabilities_for_config(local_config: Dict[str, Any]) -> ModelCapabilities:
    model_name = normalize_model_name((local_config or {}).get("model"))
    provider = infer_provider_id(
        model_name,
        configured_bot_type=(local_config or {}).get("bot_type") or "",
        use_linkai=bool((local_config or {}).get("use_linkai", False)),
        has_linkai_key=bool((local_config or {}).get("linkai_api_key")),
    )
    api_base = (local_config or {}).get("open_ai_api_base")
    provider = resolve_capability_provider_for_base(provider, api_base)
    return get_model_capabilities(model_name, provider=provider)


def _remove(payload: Dict[str, Any], keys: Iterable[str]) -> Tuple[str, ...]:
    removed = []
    for key in keys:
        if key in payload:
            payload.pop(key, None)
            removed.append(key)
    return tuple(removed)


def sanitize_chat_payload(
    payload: Dict[str, Any],
    capabilities: Optional[ModelCapabilities] = None,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Return an API-safe chat payload and the stripped parameter names."""
    clean = dict(payload or {})
    caps = capabilities or get_model_capabilities(model or clean.get("model"), provider=provider)
    removed = []

    if not caps.supports_temperature:
        removed.extend(_remove(clean, ("temperature",)))
    if not caps.supports_top_p:
        removed.extend(_remove(clean, ("top_p",)))
    if not caps.supports_penalties:
        removed.extend(_remove(clean, ("frequency_penalty", "presence_penalty")))
    if not caps.supports_reasoning_effort:
        removed.extend(_remove(clean, ("reasoning_effort",)))
    if not caps.supports_verbosity:
        removed.extend(_remove(clean, ("verbosity",)))
    if caps.max_tokens_param != "max_tokens" and "max_tokens" in clean:
        if caps.max_tokens_param:
            clean.setdefault(caps.max_tokens_param, clean.get("max_tokens"))
        removed.extend(_remove(clean, ("max_tokens",)))
    if clean.get("stream") and caps.supports_stream_usage:
        stream_options = clean.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        stream_options.setdefault("include_usage", True)
        clean["stream_options"] = stream_options

    return clean, tuple(removed)


def normalize_reasoning_effort(
    value: Any,
    capabilities: Optional[ModelCapabilities] = None,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[str]:
    """Return a provider-safe reasoning effort value, or None when unsupported."""
    caps = capabilities or get_model_capabilities(model, provider=provider)
    if not caps.supports_reasoning_effort:
        return None
    requested = str(value or "").strip().lower()
    if not requested:
        return None
    allowed = tuple(caps.reasoning_effort_values or ())
    if requested in allowed:
        return requested
    if requested in ("xhigh", "max") and "high" in allowed:
        return "high"
    if requested == "minimal" and "low" in allowed:
        return "low"
    if "high" in allowed:
        return "high"
    return allowed[-1] if allowed else requested

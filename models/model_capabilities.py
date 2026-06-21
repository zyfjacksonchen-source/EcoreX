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
)


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
    reasoning_style = "none"

    if provider_id in (const.OPENAI, const.OPEN_AI, const.CHATGPT) and _fixed_sampling_model(model_name):
        supports_temperature = False
        supports_top_p = False
        supports_penalties = False
        reasoning_style = "reasoning_effort"
    if model_name.lower().startswith("o1"):
        supports_system_messages = False

    supports_stream_usage = provider_id in (const.OPENAI, const.OPEN_AI, const.CHATGPT)
    return ModelCapabilities(
        provider=provider_id,
        model=model_name,
        supports_temperature=supports_temperature,
        supports_top_p=supports_top_p,
        supports_penalties=supports_penalties,
        supports_stream_usage=supports_stream_usage,
        supports_system_messages=supports_system_messages,
        reasoning_style=reasoning_style,
    )


def capabilities_for_config(local_config: Dict[str, Any]) -> ModelCapabilities:
    model_name = normalize_model_name((local_config or {}).get("model"))
    provider = infer_provider_id(
        model_name,
        configured_bot_type=(local_config or {}).get("bot_type") or "",
        use_linkai=bool((local_config or {}).get("use_linkai", False)),
        has_linkai_key=bool((local_config or {}).get("linkai_api_key")),
    )
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
    if clean.get("stream") and caps.supports_stream_usage:
        stream_options = clean.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        stream_options.setdefault("include_usage", True)
        clean["stream_options"] = stream_options

    return clean, tuple(removed)

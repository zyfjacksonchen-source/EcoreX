# encoding:utf-8
"""Central model capability helpers for OpenAI-compatible chat calls.

The runtime supports many providers through OpenAI-shaped APIs, but not every
model accepts the same request parameters. Keep the shared rules here so agent
calls, model settings, and tests do not each grow their own model-name checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

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
    context_window_tokens: int = 258000
    max_output_tokens: int = 32000
    auto_compact_token_limit: int = 206400
    hard_context_token_limit: int = 237360
    context_policy_source: str = "ecorex:model-context-policy:v1"
    tokenizer: str = "heuristic"
    tokenizer_status: str = "estimated"
    tokenizer_note: str = "No provider tokenizer is wired; EcoreX uses a conservative estimate."

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelContextPolicy:
    provider: str
    model: str
    context_window_tokens: int
    max_output_tokens: int
    auto_compact_token_limit: int
    hard_context_token_limit: int
    source: str = "ecorex:model-context-policy:v1"
    note: str = ""
    tokenizer: str = "heuristic"
    tokenizer_status: str = "estimated"
    tokenizer_note: str = "No provider tokenizer is wired; EcoreX uses a conservative estimate."

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelCapabilityRule:
    rule_id: str
    provider: str = ""
    exact_models: Tuple[str, ...] = ()
    model_prefixes: Tuple[str, ...] = ()
    api_family: str = "native"
    host_policy: str = "provider_default"
    system_message_policy: str = "native"
    surfaces: Tuple[str, ...] = ("agent_bridge",)
    overrides: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, provider_id: str, model_name: str) -> bool:
        if self.provider and self.provider != provider_id:
            return False
        lowered_model = model_name.lower()
        if not self.exact_models and not self.model_prefixes:
            return True
        if any(lowered_model == exact.lower() for exact in self.exact_models):
            return True
        return any(lowered_model.startswith(prefix.lower()) for prefix in self.model_prefixes)

    def to_matrix_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "provider": self.provider,
            "match": {
                "exact_models": list(self.exact_models),
                "model_prefixes": list(self.model_prefixes),
                "fallback": not self.exact_models and not self.model_prefixes,
            },
            "api_family": self.api_family,
            "host_policy": self.host_policy,
            "system_message_policy": self.system_message_policy,
            "surfaces": list(self.surfaces),
            "overrides": _matrix_jsonable(dict(self.overrides)),
        }


def _matrix_jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_matrix_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_matrix_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _matrix_jsonable(item) for key, item in value.items()}
    return value


_FIXED_SAMPLING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_FIXED_SAMPLING_EXACT = {
    const.GPT_5,
    const.GPT_5_MINI,
    const.GPT_5_NANO,
    const.GPT_54,
    const.GPT_54_MINI,
    const.GPT_54_NANO,
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

_OPENAI_BASE_SENSITIVE_PROVIDERS = {
    const.OPENAI,
    const.OPEN_AI,
    const.CHATGPT,
    "openai",
}
_OFFICIAL_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"

_OPENAI_REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high")
_DEEPSEEK_REASONING_EFFORT_VALUES = ("high", "max")
_CAPABILITY_MATRIX_SCHEMA_VERSION = "ecorex.model-capabilities.v1"
_OFFICIAL_OPENAI_SURFACES = (
    "openai_compatible_bot",
    "agent_bridge",
    "legacy_chatgpt_args",
    "responses_adapter",
)
_AZURE_OPENAI_SURFACES = (
    "openai_compatible_bot",
    "agent_bridge",
    "legacy_chatgpt_args",
)
_OPENAI_COMPATIBLE_SURFACES = (
    "openai_compatible_bot",
    "agent_bridge",
    "legacy_chatgpt_args",
)
_OPENAI_RULE_PROVIDERS = tuple(dict.fromkeys((
    const.OPENAI,
    const.OPEN_AI,
    const.CHATGPT,
    "openai",
)))
_AZURE_OPENAI_RULE_PROVIDERS = (const.CHATGPTONAZURE,)

_DEFAULT_PROVIDER_CAPABILITY_MODELS: Dict[str, Tuple[str, ...]] = {
    const.OPENAI: (const.GPT_56_LUNA, const.GPT_56_SOL, const.GPT_54_MINI, const.GPT_5, "o1-mini"),
    const.CHATGPTONAZURE: (const.GPT_56_LUNA, const.GPT_56_SOL, "o1-mini"),
    const.DEEPSEEK: (const.DEEPSEEK_V4_PRO, const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER),
    const.QWEN_DASHSCOPE: (const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS),
    const.ZHIPU_AI: (const.GLM_5_1, const.GLM_5, const.GLM_4_7),
    const.MOONSHOT: (const.KIMI_K2_6, const.KIMI_K2),
    const.DOUBAO: (const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_21_PRO, const.DOUBAO_SEED_2_CODE),
    const.MIMO: (const.MIMO_V2_5_PRO, const.MIMO_V2_5),
    const.LINKAI: (const.GPT_54_MINI, const.QWEN37_PLUS, const.KIMI_K2_6),
    "custom": (),
}

_CONTEXT_POLICY_DEFAULT_WINDOW = 258000
_CONTEXT_POLICY_DEFAULT_MAX_OUTPUT = 32000
_CONTEXT_POLICY_SOFT_RATIO = 0.80
_CONTEXT_POLICY_HARD_RATIO = 0.92

_CONTEXT_EXACT: Dict[str, Tuple[int, int, str, str]] = {
    const.GPT_54: (1050000, 128000, "openai:gpt-5.4", "OpenAI model page lists 1.05M context and 128K max output."),
    const.GPT_54_MINI: (1050000, 128000, "openai:gpt-5.4-mini", "OpenAI model page lists 1.05M context and 128K max output."),
    const.GPT_54_NANO: (1050000, 128000, "openai:gpt-5.4-nano", "OpenAI model page lists 1.05M context and 128K max output."),
    const.GPT_5: (400000, 128000, "openai:gpt-5", "OpenAI model page lists 400K context and 128K max output."),
    const.GPT_5_MINI: (400000, 128000, "openai:gpt-5-mini", "OpenAI model page lists 400K context and 128K max output."),
    const.GPT_5_NANO: (400000, 128000, "openai:gpt-5-nano", "OpenAI model page lists 400K context and 128K max output."),
    const.GPT_41: (1000000, 32768, "openai:gpt-4.1", "OpenAI GPT-4.1 family is treated as long-context."),
    const.GPT_41_MINI: (1000000, 32768, "openai:gpt-4.1-mini", "OpenAI GPT-4.1 family is treated as long-context."),
    const.GPT_41_NANO: (1000000, 32768, "openai:gpt-4.1-nano", "OpenAI GPT-4.1 family is treated as long-context."),
    const.GEMINI_31_PRO_PRE: (1048576, 65536, "google:gemini-3.1-pro-preview", "Google model page lists 1,048,576 input tokens and 65,536 output tokens."),
    const.GEMINI_35_FLASH: (1048576, 65536, "google:gemini-3.5-flash", "Gemini 3 family policy uses the official 1M/64K guidance."),
    const.GEMINI_31_FLASH_LITE_PRE: (1048576, 65536, "google:gemini-3.1-flash-lite-preview", "Gemini 3 family policy uses the official 1M/64K guidance."),
    const.GEMINI_3_PRO_PRE: (1048576, 65536, "google:gemini-3-pro-preview", "Gemini 3 family policy uses the official 1M/64K guidance."),
    const.GEMINI_3_FLASH_PRE: (1048576, 65536, "google:gemini-3-flash-preview", "Gemini 3 family policy uses the official 1M/64K guidance."),
    const.DEEPSEEK_V4_PRO: (1000000, 64000, "deepseek:v4-pro", "DeepSeek V4 release notes list 1M context; output budget kept conservative."),
    const.DEEPSEEK_V4_FLASH: (1000000, 64000, "deepseek:v4-flash", "DeepSeek V4 release notes list 1M context; output budget kept conservative."),
    const.DOUBAO_SEED_21_PRO: (256000, 32000, "volcengine:doubao-seed-2.1-pro", "Doubao Seed policy uses 256K context with conservative output reserve."),
    const.DOUBAO_SEED_2_PRO: (256000, 32000, "volcengine:doubao-seed-2.0-pro", "Doubao Seed policy uses 256K context with conservative output reserve."),
    const.DOUBAO_SEED_2_CODE: (256000, 32000, "volcengine:doubao-seed-2.0-code", "Doubao Seed policy uses 256K context with conservative output reserve."),
}

_CONTEXT_PREFIXES: Tuple[Tuple[str, int, int, str, str], ...] = (
    ("gpt-5.4", 1050000, 128000, "openai:gpt-5.4", "OpenAI GPT-5.4 long-context policy."),
    ("gpt-5", 400000, 128000, "openai:gpt-5", "OpenAI GPT-5 family policy."),
    ("gpt-4.1", 1000000, 32768, "openai:gpt-4.1", "OpenAI GPT-4.1 family policy."),
    ("deepseek-v4", 1000000, 64000, "deepseek:v4", "DeepSeek V4 1M-context policy."),
    ("gemini-3", 1048576, 65536, "google:gemini-3", "Gemini 3 1M/64K-context policy."),
    ("gemini-2.5", 1048576, 65536, "google:gemini-2.5", "Gemini 2.5 long-context policy."),
    ("gemini-1.5", 1048576, 8192, "google:gemini-1.5", "Gemini 1.5 long-context policy."),
    ("doubao-seed-", 256000, 32000, "volcengine:doubao-seed", "Doubao Seed conservative context policy."),
    ("kimi-k2", 256000, 32000, "moonshot:kimi-k2", "Kimi K2 conservative context policy."),
    ("moonshot-v1-128k", 128000, 16000, "moonshot:128k", "Moonshot 128K policy."),
    ("claude-", 200000, 64000, "anthropic:claude", "Claude long-context policy."),
    ("qwen3", 128000, 32000, "dashscope:qwen3", "Qwen3 conservative context policy."),
    ("glm-5", 128000, 16000, "zhipu:glm-5", "GLM-5 conservative context policy."),
)


def _derive_context_limits(context_window: int, max_output: int) -> Tuple[int, int]:
    context_window = max(1, int(context_window or _CONTEXT_POLICY_DEFAULT_WINDOW))
    max_output = max(1, int(max_output or _CONTEXT_POLICY_DEFAULT_MAX_OUTPUT))
    output_headroom = max(8192, min(max_output, max(1, context_window // 4)))
    soft_limit = min(int(context_window * _CONTEXT_POLICY_SOFT_RATIO), context_window - output_headroom)
    hard_limit = int(context_window * _CONTEXT_POLICY_HARD_RATIO)
    hard_limit = max(1, min(context_window - 1 if context_window > 1 else 1, hard_limit))
    soft_limit = max(1, min(soft_limit, hard_limit))
    return soft_limit, hard_limit


def _tokenizer_policy_for_model(model_name: str, provider_id: str) -> Tuple[str, str, str]:
    lowered = (model_name or "").lower()
    if (
        provider_id in {const.OPENAI, const.OPEN_AI, const.CHATGPT, "openai", const.CHATGPTONAZURE}
        or lowered.startswith(("gpt-", "o1", "o3", "o4"))
    ):
        return (
            "tiktoken",
            "local_tokenizer",
            "OpenAI-family models use local tiktoken when the package is available; otherwise EcoreX falls back to a conservative estimate.",
        )
    if lowered.startswith("gemini"):
        return (
            "provider_count_tokens",
            "estimated",
            "Gemini exposes a countTokens API, but EcoreX does not call it on every keystroke; live UI uses conservative estimation until usage is returned.",
        )
    if lowered.startswith("deepseek"):
        return (
            "deepseek_encoding_estimate",
            "estimated",
            "DeepSeek V4 uses provider-specific encoding; EcoreX keeps a conservative local estimate until provider usage is returned.",
        )
    if lowered.startswith("doubao-seed"):
        return (
            "ark_token_estimate",
            "estimated",
            "Volcengine Ark tokenizer is not bundled; EcoreX keeps a conservative local estimate until provider usage is returned.",
        )
    return (
        "heuristic",
        "estimated",
        "No provider tokenizer is wired; EcoreX uses a conservative estimate.",
    )


def context_policy_for_model(model: Optional[str], provider: Optional[str] = None) -> ModelContextPolicy:
    model_name = normalize_model_name(model)
    provider_id = provider or infer_provider_id(model_name)
    lowered = model_name.lower()
    window = _CONTEXT_POLICY_DEFAULT_WINDOW
    max_output = _CONTEXT_POLICY_DEFAULT_MAX_OUTPUT
    source = "ecorex:default-context-policy"
    note = "Fallback policy used when the provider does not publish a known limit in EcoreX."

    exact = _CONTEXT_EXACT.get(model_name)
    if exact is None:
        exact = _CONTEXT_EXACT.get(lowered)
    if exact:
        window, max_output, source, note = exact
    else:
        for prefix, prefix_window, prefix_output, prefix_source, prefix_note in _CONTEXT_PREFIXES:
            if lowered.startswith(prefix):
                window, max_output, source, note = prefix_window, prefix_output, prefix_source, prefix_note
                break

    auto_limit, hard_limit = _derive_context_limits(window, max_output)
    tokenizer, tokenizer_status, tokenizer_note = _tokenizer_policy_for_model(model_name, provider_id)
    return ModelContextPolicy(
        provider=provider_id,
        model=model_name,
        context_window_tokens=int(window),
        max_output_tokens=int(max_output),
        auto_compact_token_limit=int(auto_limit),
        hard_context_token_limit=int(hard_limit),
        source=source,
        note=note,
        tokenizer=tokenizer,
        tokenizer_status=tokenizer_status,
        tokenizer_note=tokenizer_note,
    )

_CAPABILITY_RULES: Tuple[ModelCapabilityRule, ...] = (
    ModelCapabilityRule(
        rule_id="default:native-compatible",
        api_family="native",
        host_policy="provider_default",
    ),
    *(
        ModelCapabilityRule(
            rule_id=f"{provider_id}:official-openai-base",
            provider=provider_id,
            api_family="official_openai",
            host_policy="official_openai_host_required",
            surfaces=_OFFICIAL_OPENAI_SURFACES,
            overrides={"supports_stream_usage": True},
        )
        for provider_id in _OPENAI_RULE_PROVIDERS
    ),
    *(
        ModelCapabilityRule(
            rule_id=f"{provider_id}:azure-openai-base",
            provider=provider_id,
            api_family="azure_openai",
            host_policy="azure_deployment",
            surfaces=_AZURE_OPENAI_SURFACES,
            overrides={"supports_stream_usage": True},
        )
        for provider_id in _AZURE_OPENAI_RULE_PROVIDERS
    ),
    ModelCapabilityRule(
        rule_id="openai_compatible:generic-compatible-base",
        provider="openai_compatible",
        api_family="openai_compatible",
        host_policy="generic_compatible_host",
        surfaces=_OPENAI_COMPATIBLE_SURFACES,
    ),
    ModelCapabilityRule(
        rule_id="custom:generic-compatible-base",
        provider="custom",
        api_family="openai_compatible",
        host_policy="custom_host",
        surfaces=_OPENAI_COMPATIBLE_SURFACES,
    ),
    *(
        ModelCapabilityRule(
            rule_id=f"{provider_id}:fixed-sampling-reasoning-models",
            provider=provider_id,
            exact_models=tuple(_FIXED_SAMPLING_EXACT),
            model_prefixes=_FIXED_SAMPLING_PREFIXES,
            api_family="official_openai" if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else "azure_openai",
            host_policy="official_openai_host_required" if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else "azure_deployment",
            surfaces=_OFFICIAL_OPENAI_SURFACES if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else _AZURE_OPENAI_SURFACES,
            overrides={
                "supports_temperature": False,
                "supports_top_p": False,
                "supports_penalties": False,
                "supports_reasoning_effort": True,
                "supports_verbosity": True,
                "reasoning_effort_values": _OPENAI_REASONING_EFFORT_VALUES,
                "reasoning_style": "reasoning_effort",
                "max_tokens_param": "max_completion_tokens",
            },
        )
        for provider_id in (*_OPENAI_RULE_PROVIDERS, *_AZURE_OPENAI_RULE_PROVIDERS)
    ),
    *(
        ModelCapabilityRule(
            rule_id=f"{provider_id}:o1-system-message-coercion",
            provider=provider_id,
            model_prefixes=("o1",),
            api_family="official_openai" if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else "azure_openai",
            host_policy="official_openai_host_required" if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else "azure_deployment",
            system_message_policy="coerce_to_user",
            surfaces=_OFFICIAL_OPENAI_SURFACES if provider_id not in _AZURE_OPENAI_RULE_PROVIDERS else _AZURE_OPENAI_SURFACES,
            overrides={"supports_system_messages": False},
        )
        for provider_id in (*_OPENAI_RULE_PROVIDERS, *_AZURE_OPENAI_RULE_PROVIDERS)
    ),
    ModelCapabilityRule(
        rule_id="deepseek:v4-thinking",
        provider=const.DEEPSEEK,
        model_prefixes=("deepseek-v4",),
        api_family="native",
        overrides={
            "supports_temperature": False,
            "supports_top_p": False,
            "supports_penalties": False,
            "supports_reasoning_effort": True,
            "supports_thinking_param": True,
            "reasoning_effort_values": _DEEPSEEK_REASONING_EFFORT_VALUES,
            "reasoning_style": "thinking",
        },
    ),
    ModelCapabilityRule(
        rule_id="dashscope:qwen-thinking",
        provider=const.QWEN_DASHSCOPE,
        model_prefixes=("qwen3", "qwq"),
        api_family="native",
        overrides={"supports_thinking_param": True, "reasoning_style": "thinking"},
    ),
    ModelCapabilityRule(
        rule_id="zhipu:glm-thinking",
        provider=const.ZHIPU_AI,
        model_prefixes=("glm-4.7", "glm-5"),
        api_family="native",
        overrides={"supports_thinking_param": True, "reasoning_style": "thinking"},
    ),
    ModelCapabilityRule(
        rule_id="moonshot:kimi-thinking",
        provider=const.MOONSHOT,
        model_prefixes=("kimi-k2", "kimi-k1.5"),
        api_family="native",
        overrides={"supports_thinking_param": True, "reasoning_style": "thinking"},
    ),
    ModelCapabilityRule(
        rule_id="mimo:thinking",
        provider=const.MIMO,
        model_prefixes=("mimo-",),
        api_family="native",
        overrides={
            "supports_thinking_param": True,
            "reasoning_style": "thinking",
            "max_tokens_param": "max_completion_tokens",
        },
    ),
    ModelCapabilityRule(
        rule_id="doubao:seed-thinking",
        provider=const.DOUBAO,
        model_prefixes=("doubao-seed-",),
        api_family="native",
        overrides={
            "supports_temperature": False,
            "supports_thinking_param": True,
            "reasoning_style": "thinking",
        },
    ),
)


def normalize_model_name(model: Optional[str]) -> str:
    return str(model or "").strip()


def is_official_gemini_api_base(api_base: Optional[str]) -> bool:
    """Return whether a Gemini REST base is the public Google endpoint."""
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return True
    return base == _OFFICIAL_GEMINI_API_BASE.rstrip("/")


def is_custom_gemini_transport(
    model: Optional[str],
    *,
    configured_bot_type: Optional[str] = "",
    gemini_api_base: Optional[str] = "",
    has_gemini_key: bool = False,
) -> bool:
    """Detect Gemini-named models explicitly configured as custom OpenAI API.

    A non-Google ``gemini_api_base`` is still a Gemini REST endpoint in the
    deployed EcoreX environment; routing it through OpenAI-compatible
    ``/chat/completions`` causes the provider to fail. Only an explicit
    ``bot_type=custom`` should use the custom OpenAI-compatible transport.
    """
    model_name = normalize_model_name(model).lower()
    if not model_name.startswith("gemini"):
        return False
    route = str(configured_bot_type or "").strip()
    return bool(route == const.CUSTOM and has_gemini_key)


def should_route_custom_gemini_as_rest(
    model: Optional[str],
    *,
    configured_bot_type: Optional[str] = "",
    gemini_api_base: Optional[str] = "",
    gemini_api_key: Optional[str] = "",
    custom_api_base: Optional[str] = "",
    custom_api_key: Optional[str] = "",
) -> bool:
    """Repair v0.2.7.1 custom-Gemini configs that copied Gemini REST fields.

    The failed v0.2.7.1 path migrated ``gemini_*`` credentials into
    ``custom_*`` and then called ``/chat/completions``. If the custom fields
    are absent or exactly mirror the Gemini REST fields, route back to the
    Gemini REST bot. Real custom OpenAI-compatible Gemini deployments keep
    distinct ``custom_*`` credentials and remain on ``provider=custom``.
    """
    model_name = normalize_model_name(model).lower()
    if not model_name.startswith("gemini"):
        return False
    if str(configured_bot_type or "").strip() != const.CUSTOM:
        return False
    gemini_key = str(gemini_api_key or "").strip()
    gemini_base = str(gemini_api_base or "").strip()
    if not gemini_key or not gemini_base:
        return False
    custom_key = str(custom_api_key or "").strip()
    custom_base = str(custom_api_base or "").strip()
    if not custom_key and not custom_base:
        return True
    normalized_gemini_base = normalize_openai_compatible_api_base(gemini_base).rstrip("/")
    normalized_custom_base = custom_base.rstrip("/")
    key_matches = not custom_key or custom_key == gemini_key
    base_matches = not normalized_custom_base or normalized_custom_base == normalized_gemini_base
    return bool(key_matches and base_matches)


def normalize_openai_compatible_api_base(api_base: Optional[str]) -> str:
    """Normalize a custom OpenAI-compatible base without rewriting routed paths."""
    raw = str(api_base or "").strip()
    if not raw:
        return ""
    value = raw.rstrip("/")
    lowered = value.lower()
    if lowered.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")].rstrip("/")
    try:
        parsed = urlsplit(value)
    except Exception:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    path = (parsed.path or "").rstrip("/")
    if not path:
        return urlunsplit((parsed.scheme, parsed.netloc, "/v1", parsed.query, parsed.fragment))
    return value


def infer_provider_id(
    model: Optional[str],
    *,
    configured_bot_type: Optional[str] = "",
    use_linkai: bool = False,
    has_linkai_key: bool = False,
    use_azure_chatgpt: bool = False,
    gemini_api_base: Optional[str] = "",
    has_gemini_key: bool = False,
    gemini_api_key: Optional[str] = "",
    custom_api_base: Optional[str] = "",
    custom_api_key: Optional[str] = "",
) -> str:
    """Infer the provider id used by the local runtime for a chat model."""
    if use_linkai and has_linkai_key:
        return const.LINKAI
    if should_route_custom_gemini_as_rest(
        model,
        configured_bot_type=configured_bot_type,
        gemini_api_base=gemini_api_base,
        gemini_api_key=gemini_api_key,
        custom_api_base=custom_api_base,
        custom_api_key=custom_api_key,
    ):
        return const.GEMINI
    if is_custom_gemini_transport(
        model,
        configured_bot_type=configured_bot_type,
        gemini_api_base=gemini_api_base,
        has_gemini_key=has_gemini_key,
    ):
        return const.CUSTOM
    if configured_bot_type:
        return const.OPENAI if configured_bot_type == const.CHATGPT else str(configured_bot_type)
    if use_azure_chatgpt:
        return const.CHATGPTONAZURE

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


def _matching_capability_rules(provider_id: str, model_name: str) -> Tuple[ModelCapabilityRule, ...]:
    return tuple(rule for rule in _CAPABILITY_RULES if rule.matches(provider_id, model_name))


def get_model_capabilities(model: Optional[str], provider: Optional[str] = None) -> ModelCapabilities:
    model_name = normalize_model_name(model)
    provider_id = provider or infer_provider_id(model_name)
    capabilities = ModelCapabilities(
        provider=provider_id,
        model=model_name,
    )
    for rule in _matching_capability_rules(provider_id, model_name):
        capabilities = replace(capabilities, **dict(rule.overrides))
    context_policy = context_policy_for_model(model_name, provider_id)
    capabilities = replace(
        capabilities,
        context_window_tokens=context_policy.context_window_tokens,
        max_output_tokens=context_policy.max_output_tokens,
        auto_compact_token_limit=context_policy.auto_compact_token_limit,
        hard_context_token_limit=context_policy.hard_context_token_limit,
        context_policy_source=context_policy.source,
        tokenizer=context_policy.tokenizer,
        tokenizer_status=context_policy.tokenizer_status,
        tokenizer_note=context_policy.tokenizer_note,
    )
    return capabilities


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
        use_azure_chatgpt=bool((local_config or {}).get("use_azure_chatgpt", False)),
        gemini_api_base=(local_config or {}).get("gemini_api_base") or "",
        has_gemini_key=bool((local_config or {}).get("gemini_api_key")),
        gemini_api_key=(local_config or {}).get("gemini_api_key") or "",
        custom_api_base=(local_config or {}).get("custom_api_base") or "",
        custom_api_key=(local_config or {}).get("custom_api_key") or "",
    )
    api_base = (local_config or {}).get("open_ai_api_base")
    provider = resolve_capability_provider_for_base(provider, api_base)
    return get_model_capabilities(model_name, provider=provider)


def _catalog_model_value(entry: Any) -> str:
    if isinstance(entry, Mapping):
        return normalize_model_name(entry.get("value"))
    return normalize_model_name(entry)


def _rule_summary_value(
    matched_rules: Tuple[ModelCapabilityRule, ...],
    attr: str,
    default: str,
) -> str:
    for rule in reversed(matched_rules):
        value = getattr(rule, attr)
        if value != default:
            return value
    return default


def _capability_matrix_row(
    provider_id: str,
    model_name: str,
    capabilities: ModelCapabilities,
    matched_rules: Tuple[ModelCapabilityRule, ...],
) -> Dict[str, Any]:
    return {
        "provider_id": provider_id,
        "model": model_name,
        "api_family": _rule_summary_value(matched_rules, "api_family", "native"),
        "host_policy": _rule_summary_value(matched_rules, "host_policy", "provider_default"),
        "system_message_policy": "native" if capabilities.supports_system_messages else "coerce_to_user",
        "supports_tools": capabilities.supports_tools,
        "supports_stream": capabilities.supports_stream,
        "supports_stream_usage": capabilities.supports_stream_usage,
        "sampling": {
            "temperature": capabilities.supports_temperature,
            "top_p": capabilities.supports_top_p,
            "frequency_penalty": capabilities.supports_penalties,
            "presence_penalty": capabilities.supports_penalties,
        },
        "unsupported_params": [
            name for name, supported in (
                ("temperature", capabilities.supports_temperature),
                ("top_p", capabilities.supports_top_p),
                ("frequency_penalty", capabilities.supports_penalties),
                ("presence_penalty", capabilities.supports_penalties),
                ("reasoning_effort", capabilities.supports_reasoning_effort),
                ("verbosity", capabilities.supports_verbosity),
            )
            if not supported
        ],
        "token_limit": {
            "chat_param": capabilities.max_tokens_param,
            "responses_param": "max_output_tokens",
            "context_window_tokens": capabilities.context_window_tokens,
            "max_output_tokens": capabilities.max_output_tokens,
            "auto_compact_token_limit": capabilities.auto_compact_token_limit,
            "hard_context_token_limit": capabilities.hard_context_token_limit,
            "source": capabilities.context_policy_source,
            "tokenizer": capabilities.tokenizer,
            "tokenizer_status": capabilities.tokenizer_status,
        },
        "context_policy": {
            "context_window_tokens": capabilities.context_window_tokens,
            "max_output_tokens": capabilities.max_output_tokens,
            "auto_compact_token_limit": capabilities.auto_compact_token_limit,
            "hard_context_token_limit": capabilities.hard_context_token_limit,
            "source": capabilities.context_policy_source,
            "tokenizer": capabilities.tokenizer,
            "tokenizer_status": capabilities.tokenizer_status,
            "tokenizer_note": capabilities.tokenizer_note,
        },
        "reasoning": {
            "supported": capabilities.supports_reasoning_effort,
            "style": capabilities.reasoning_style,
            "field": "reasoning_effort" if capabilities.supports_reasoning_effort else "",
            "allowed_values": list(capabilities.reasoning_effort_values or ()),
        },
        "verbosity": {
            "supported": capabilities.supports_verbosity,
            "field": "verbosity" if capabilities.supports_verbosity else "",
        },
        "thinking": {
            "supported": capabilities.supports_thinking_param,
            "field": "thinking" if capabilities.supports_thinking_param else "",
            "enabled_shape": {"type": "enabled"} if capabilities.supports_thinking_param else {},
        },
        "surfaces": sorted({
            surface
            for rule in matched_rules
            for surface in rule.surfaces
        }),
        "capabilities": capabilities.to_dict(),
        "rule_ids": [rule.rule_id for rule in matched_rules],
        "rules": [rule.to_matrix_dict() for rule in matched_rules],
    }


def build_provider_capability_matrix(
    provider_models: Optional[Mapping[str, Iterable[Any]]] = None,
) -> Dict[str, Any]:
    """Build a machine-readable provider/model capability matrix."""
    catalog = provider_models or _DEFAULT_PROVIDER_CAPABILITY_MODELS
    providers: Dict[str, Any] = {}
    for provider_id, entries in catalog.items():
        seen = set()
        models = []
        for entry in entries or ():
            model_name = _catalog_model_value(entry)
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            matched_rules = _matching_capability_rules(str(provider_id), model_name)
            capabilities = get_model_capabilities(model_name, provider=provider_id)
            models.append(_capability_matrix_row(
                str(provider_id),
                model_name,
                capabilities,
                matched_rules,
            ))
        providers[str(provider_id)] = {
            "provider": str(provider_id),
            "models": models,
        }
    return {
        "schema_version": _CAPABILITY_MATRIX_SCHEMA_VERSION,
        "source": "models.model_capabilities._CAPABILITY_RULES",
        "providers": providers,
    }


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

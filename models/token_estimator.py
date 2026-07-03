# encoding:utf-8
"""Provider-aware local token estimation.

This module deliberately separates local estimates from provider usage. The
only exact post-call source remains the model API's usage payload; pre-call
budgeting uses the best local tokenizer available and records its confidence
through model_capabilities.context_policy_for_model().
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Optional

from common import const


@lru_cache(maxsize=64)
def _tiktoken_encoding(model_name: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model_name)
    except Exception:
        for name in ("o200k_base", "cl100k_base"):
            try:
                return tiktoken.get_encoding(name)
            except Exception:
                continue
        raise


def _heuristic_tokens(text: str, *, multiplier: float = 1.0) -> int:
    if not text:
        return 0
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_count = len(text) - non_ascii
    whitespace = sum(1 for char in text if char.isspace())
    structural = sum(1 for char in text if char in "{}[]():,.;/\\|`'\"")
    # Conservative mixed-language estimate. ASCII/code is close to 4 chars per
    # token; CJK often lands closer to 1-2 chars per token depending on model.
    base = non_ascii * 1.2 + max(0, ascii_count - whitespace) * 0.27 + whitespace * 0.12 + structural * 0.08
    return max(1, int(math.ceil(base * multiplier)) + 1)


def estimate_text_tokens(text: Any, model: Optional[str] = None, provider: Optional[str] = None) -> int:
    raw = "" if text is None else str(text)
    if not raw:
        return 0

    model_name = str(model or "").strip()
    provider_id = str(provider or "").strip()
    lowered = model_name.lower()
    openai_family = (
        provider_id in {const.OPENAI, const.OPEN_AI, const.CHATGPT, "openai", const.CHATGPTONAZURE}
        or lowered.startswith(("gpt-", "o1", "o3", "o4"))
    )
    if openai_family:
        try:
            return max(1, len(_tiktoken_encoding(model_name or "gpt-4o").encode(raw)))
        except Exception:
            return _heuristic_tokens(raw, multiplier=1.05)

    if lowered.startswith("deepseek"):
        return _heuristic_tokens(raw, multiplier=1.10)
    if lowered.startswith("gemini"):
        return _heuristic_tokens(raw, multiplier=1.08)
    if lowered.startswith("doubao-seed"):
        return _heuristic_tokens(raw, multiplier=1.12)
    return _heuristic_tokens(raw)

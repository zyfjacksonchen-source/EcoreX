# encoding:utf-8
"""Telemetry wrapper for legacy ``reply_text`` model calls.

Most production chat paths now use ``call_with_tools`` and the shared model
gateway. A few legacy flows still call provider-specific ``reply_text``
methods directly. This wrapper records those calls without changing provider
retry behavior or forcing every adapter to be rewritten in one slice.
"""

from __future__ import annotations

import inspect
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from models.model_telemetry import ModelCallSpan


LEGACY_REPLY_TEXT_API_PATH = "/legacy/reply_text"
_suppression_state = threading.local()
_ZERO_TOKEN_TEXT_SUCCESS_PROVIDERS = {"modelscope"}


@contextmanager
def suppress_legacy_reply_text_telemetry():
    """Temporarily bypass legacy reply_text telemetry in this thread.

    Native ``call_with_tools`` adapters sometimes call ``self.reply_text`` as an
    internal implementation detail. Their public AgentBridge call is already
    covered by the native model gateway, so the inner legacy call must stay
    behaviorally intact without recording a duplicate span.
    """
    depth = int(getattr(_suppression_state, "depth", 0) or 0)
    _suppression_state.depth = depth + 1
    try:
        yield
    finally:
        if depth <= 0:
            try:
                delattr(_suppression_state, "depth")
            except AttributeError:
                pass
        else:
            _suppression_state.depth = depth


def _is_suppressed() -> bool:
    return int(getattr(_suppression_state, "depth", 0) or 0) > 0


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _legacy_usage(result: Any) -> Dict[str, int]:
    if not isinstance(result, dict):
        return {}

    usage = result.get("usage")
    if isinstance(usage, dict):
        return usage

    total_tokens = _coerce_int(result.get("total_tokens"))
    output_tokens = _coerce_int(
        result.get("completion_tokens")
        if result.get("completion_tokens") is not None
        else result.get("output_tokens")
    )
    input_tokens = _coerce_int(result.get("prompt_tokens") or result.get("input_tokens"))
    if input_tokens <= 0 and total_tokens > 0 and output_tokens <= total_tokens:
        input_tokens = total_tokens - output_tokens

    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": _coerce_int(result.get("reasoning_tokens")),
        "cached_tokens": _coerce_int(result.get("cached_tokens")),
    }


def _allows_zero_token_text_success(provider: str) -> bool:
    return str(provider or "").lower() in _ZERO_TOKEN_TEXT_SUCCESS_PROVIDERS


def _is_zero_token_text_success(provider: str, result: Dict[str, Any]) -> bool:
    return _allows_zero_token_text_success(provider) and "total_tokens" in result


def _legacy_error_details(result: Any, *, provider: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return {
            "message": f"Legacy reply_text returned unsupported response: {type(result).__name__}",
            "status_code": 500,
        }

    error_value = result.get("error")
    status_code = result.get("status_code")
    try:
        status_int = int(status_code)
    except (TypeError, ValueError):
        status_int = None

    if error_value or (status_int is not None and status_int >= 400):
        message = result.get("message") or result.get("content") or error_value
        error_code = result.get("error_code") or result.get("code") or ""
        error_type = result.get("error_type") or result.get("type") or ""
        if isinstance(error_value, dict):
            message = error_value.get("message") or message
            error_code = error_value.get("code") or error_code
            error_type = error_value.get("type") or error_type
        return {
            "message": str(message or ""),
            "status_code": result.get("status_code"),
            "error_code": str(error_code or ""),
            "error_type": str(error_type or ""),
        }

    if "completion_tokens" in result and _coerce_int(result.get("completion_tokens")) <= 0:
        content = str(result.get("content") or result.get("message") or "")
        if content and _is_zero_token_text_success(provider, result):
            return None
        return {
            "message": content or "Legacy reply_text returned no completion tokens",
            "status_code": result.get("status_code"),
            "error_code": str(result.get("error_code") or result.get("code") or ""),
            "error_type": str(result.get("error_type") or result.get("type") or ""),
        }

    return None


def _resolve_config(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Dict[str, str]:
    config: Dict[str, Any] = {}
    get_api_config = getattr(bot, "get_api_config", None)
    if callable(get_api_config):
        try:
            loaded = get_api_config()
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception:
            pass

    provider = provider_hint or str(config.get("provider") or "")
    api_base = str(config.get("api_base") or "")
    if not provider and api_base and "openai.com" not in api_base.lower():
        provider = "custom"
    if not provider:
        provider = type(bot).__name__.replace("Bot", "").lower()

    model = model_hint or str(config.get("model") or "")
    if not model:
        args = getattr(bot, "args", None)
        if isinstance(args, dict):
            model = str(args.get("model") or "")
    if not model:
        try:
            from config import conf

            model = str(conf().get("model") or "")
        except Exception:
            model = ""

    return {"provider": provider, "model": model}


def _extract_retry_count(original, args: tuple, kwargs: dict) -> int:
    if "retry_count" in kwargs:
        return _coerce_int(kwargs.get("retry_count"))
    try:
        bound = inspect.signature(original).bind_partial(*args, **kwargs)
        if "retry_count" in bound.arguments:
            return _coerce_int(bound.arguments.get("retry_count"))
    except (TypeError, ValueError):
        pass
    return 0


def wrap_legacy_reply_text(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Any:
    """Wrap ``bot.reply_text`` with bounded model-call telemetry.

    The wrapper is intentionally transparent:
    - provider-specific retry recursion remains owned by the provider adapter;
    - nested recursive ``self.reply_text(...)`` calls bypass telemetry to avoid
      duplicate spans for a single public legacy request;
    - return values and exceptions are preserved.
    """
    original = getattr(bot, "reply_text", None)
    if not callable(original) or getattr(original, "_ecorex_legacy_reply_gateway", False):
        return bot

    state = threading.local()

    def wrapped_reply_text(*args, **kwargs):
        if _is_suppressed() or getattr(state, "active", False):
            return original(*args, **kwargs)

        config = _resolve_config(bot, provider_hint=provider_hint, model_hint=model_hint)
        span = ModelCallSpan(
            provider=config["provider"],
            model=config["model"],
            stream=False,
            retry_count=_extract_retry_count(original, args, kwargs),
            api_path=LEGACY_REPLY_TEXT_API_PATH,
        )

        state.active = True
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            span.finish_error(message=str(exc), status_code=500)
            raise
        finally:
            state.active = False

        span.observe_usage(_legacy_usage(result))
        details = _legacy_error_details(result, provider=config["provider"])
        if details is None:
            span.finish_completed()
        else:
            span.finish_error(**details)
        return result

    wrapped_reply_text._ecorex_legacy_reply_gateway = True
    wrapped_reply_text._ecorex_legacy_reply_original = original
    bot.reply_text = wrapped_reply_text
    return bot

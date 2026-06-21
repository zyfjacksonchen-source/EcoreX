# encoding:utf-8
"""Telemetry wrapper for legacy model-call surfaces.

Most production chat paths now use ``call_with_tools`` and the shared model
gateway. A few legacy flows still call provider-specific ``reply_text`` or
``call_vision`` methods directly. These wrappers record those calls without
changing provider retry behavior or forcing every adapter to be rewritten in
one slice.
"""

from __future__ import annotations

import inspect
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from models.model_telemetry import ModelCallSpan


LEGACY_REPLY_TEXT_API_PATH = "/legacy/reply_text"
LEGACY_CALL_VISION_API_PATH = "/legacy/call_vision"
LEGACY_CREATE_IMAGE_API_PATH = "/legacy/create_img"
_suppression_state = threading.local()
_ZERO_TOKEN_TEXT_SUCCESS_PROVIDERS = {"modelscope"}
_DEFAULT_IMAGE_MODELS = {
    "linkai": "gpt-image-2-pro",
    "zhipu": "cogview-3",
    "zhipuai": "cogview-3",
    "zhipu_ai": "cogview-3",
}


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


def _extract_http_status_from_message(message: Any) -> Optional[str]:
    if not isinstance(message, str):
        return None
    match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _first_numeric_status(*values: Any) -> Optional[int]:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _legacy_vision_error_details(result: Any) -> Optional[Dict[str, Any]]:
    if result is NotImplemented:
        return {
            "message": "Legacy call_vision returned NotImplemented",
            "status_code": 501,
        }
    if not isinstance(result, dict):
        return {
            "message": f"Legacy call_vision returned unsupported response: {type(result).__name__}",
            "status_code": 500,
        }

    error_value = result.get("error")
    message = result.get("message") or result.get("content") or error_value
    status_code = _first_numeric_status(
        result.get("status_code"),
        result.get("http_code"),
        result.get("status"),
    )
    if status_code is None and isinstance(message, str):
        status_code = _extract_http_status_from_message(message)
    try:
        status_int = int(status_code)
    except (TypeError, ValueError):
        status_int = None

    if error_value or (status_int is not None and status_int >= 400):
        error_code = result.get("error_code") or result.get("code") or ""
        error_type = result.get("error_type") or result.get("type") or ""
        if isinstance(error_value, dict):
            message = error_value.get("message") or message
            error_code = error_value.get("code") or error_code
            error_type = error_value.get("type") or error_type
            if status_code is None:
                status_code = _first_numeric_status(
                    error_value.get("status_code"),
                    error_value.get("http_code"),
                    error_value.get("status"),
                )
        return {
            "message": str(message or ""),
            "status_code": status_code,
            "error_code": str(error_code or ""),
            "error_type": str(error_type or ""),
        }
    if "content" in result and not result.get("content"):
        return {
            "message": "Legacy call_vision returned empty content",
            "status_code": status_code,
            "error_code": str(result.get("error_code") or result.get("code") or ""),
            "error_type": str(result.get("error_type") or result.get("type") or ""),
        }
    return None


def _legacy_create_img_error_details(result: Any) -> Optional[Dict[str, Any]]:
    if result is NotImplemented:
        return {
            "message": "Legacy create_img returned NotImplemented",
            "status_code": 501,
        }
    if not isinstance(result, (tuple, list)):
        return {
            "message": f"Legacy create_img returned unsupported response: {type(result).__name__}",
            "status_code": 500,
        }
    if not result:
        return {
            "message": "Legacy create_img returned empty result",
            "status_code": 500,
        }
    if bool(result[0]):
        return None

    payload = result[1] if len(result) > 1 else "Legacy create_img failed"
    message = payload
    status_code = None
    error_code = ""
    error_type = ""
    if isinstance(payload, dict):
        error_value = payload.get("error")
        message = payload.get("message") or payload.get("content") or error_value
        status_code = _first_numeric_status(
            payload.get("status_code"),
            payload.get("http_code"),
            payload.get("status"),
        )
        error_code = payload.get("error_code") or payload.get("code") or ""
        error_type = payload.get("error_type") or payload.get("type") or ""
        if isinstance(error_value, dict):
            message = error_value.get("message") or message
            error_code = error_value.get("code") or error_code
            error_type = error_value.get("type") or error_type
            if status_code in (None, ""):
                status_code = _first_numeric_status(
                    error_value.get("status_code"),
                    error_value.get("http_code"),
                    error_value.get("status"),
                )
    if status_code in (None, ""):
        status_code = _extract_http_status_from_message(message)

    return {
        "message": str(message or "Legacy create_img failed"),
        "status_code": status_code,
        "error_code": str(error_code or ""),
        "error_type": str(error_type or ""),
    }


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


def _resolve_image_config(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Dict[str, str]:
    config = _resolve_config(bot, provider_hint=provider_hint, model_hint=model_hint)
    if model_hint:
        config["model"] = model_hint
        return config

    image_model = ""
    try:
        from config import conf

        image_model = str(conf().get("text_to_image") or "")
    except Exception:
        image_model = ""
    if not image_model:
        default_image_model = getattr(bot, "DEFAULT_IMAGE_MODEL", "")
        if default_image_model:
            image_model = str(default_image_model)
    if not image_model:
        provider_key = str(config.get("provider") or "").lower()
        image_model = _DEFAULT_IMAGE_MODELS.get(provider_key, "")
    if image_model:
        config["model"] = image_model
    return config


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


def _extract_model_argument(original, args: tuple, kwargs: dict) -> str:
    if kwargs.get("model"):
        return str(kwargs.get("model"))
    try:
        bound = inspect.signature(original).bind_partial(*args, **kwargs)
        if bound.arguments.get("model"):
            return str(bound.arguments.get("model"))
    except (TypeError, ValueError):
        pass
    return ""


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


def wrap_legacy_call_vision(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Any:
    """Wrap ``bot.call_vision`` with bounded model-call telemetry."""
    original = getattr(bot, "call_vision", None)
    if not callable(original) or getattr(original, "_ecorex_legacy_call_vision_gateway", False):
        return bot

    state = threading.local()

    def wrapped_call_vision(*args, **kwargs):
        if getattr(state, "active", False):
            return original(*args, **kwargs)

        call_model = _extract_model_argument(original, args, kwargs)
        config = _resolve_config(
            bot,
            provider_hint=provider_hint,
            model_hint=call_model or model_hint,
        )
        span = ModelCallSpan(
            provider=config["provider"],
            model=config["model"],
            stream=False,
            retry_count=0,
            api_path=LEGACY_CALL_VISION_API_PATH,
        )

        state.active = True
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            span.finish_error(message=str(exc), status_code=500)
            raise
        finally:
            state.active = False

        if isinstance(result, dict):
            if result.get("model"):
                span.model = str(result.get("model"))
            span.observe_usage(result.get("usage"))
        details = _legacy_vision_error_details(result)
        if details is None:
            span.finish_completed()
        else:
            span.finish_error(**details)
        return result

    wrapped_call_vision._ecorex_legacy_call_vision_gateway = True
    wrapped_call_vision._ecorex_legacy_call_vision_original = original
    bot.call_vision = wrapped_call_vision
    return bot


def wrap_legacy_create_img(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Any:
    """Wrap ``bot.create_img`` with bounded model-call telemetry."""
    original = getattr(bot, "create_img", None)
    if not callable(original) or getattr(original, "_ecorex_legacy_create_img_gateway", False):
        return bot

    state = threading.local()

    def wrapped_create_img(*args, **kwargs):
        if getattr(state, "active", False):
            return original(*args, **kwargs)

        config = _resolve_image_config(bot, provider_hint=provider_hint, model_hint=model_hint)
        span = ModelCallSpan(
            provider=config["provider"],
            model=config["model"],
            stream=False,
            retry_count=_extract_retry_count(original, args, kwargs),
            api_path=LEGACY_CREATE_IMAGE_API_PATH,
        )

        state.active = True
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            span.finish_error(message=str(exc), status_code=500)
            raise
        finally:
            state.active = False

        details = _legacy_create_img_error_details(result)
        if details is None:
            span.finish_completed()
        else:
            span.finish_error(**details)
        return result

    wrapped_create_img._ecorex_legacy_create_img_gateway = True
    wrapped_create_img._ecorex_legacy_create_img_original = original
    bot.create_img = wrapped_create_img
    return bot


def wrap_legacy_model_surfaces(bot: Any, *, provider_hint: str = "", model_hint: str = "") -> Any:
    bot = wrap_legacy_reply_text(bot, provider_hint=provider_hint, model_hint=model_hint)
    bot = wrap_legacy_call_vision(bot, provider_hint=provider_hint, model_hint=model_hint)
    bot = wrap_legacy_create_img(bot, provider_hint=provider_hint, model_hint=model_hint)
    return bot

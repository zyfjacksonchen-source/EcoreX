# encoding:utf-8
"""Shared retry helpers for legacy image-generation surfaces."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from common.log import logger
from models.model_provider_errors import http_error_response, provider_error_response
from models.model_retry import build_retry_decision, coerce_max_retries, sleep_for_retry


class ModelImageCallError(Exception):
    """Exception carrying normalized provider error details."""

    def __init__(self, details: Dict[str, Any]):
        self.details = dict(details or {})
        super().__init__(self.details.get("message") or "Image generation failed")


def _provider_create_img_error_state(bot: Any):
    state = getattr(bot, "_ecorex_create_img_error_state", None)
    if state is None:
        state = threading.local()
        try:
            setattr(bot, "_ecorex_create_img_error_state", state)
        except Exception:
            return None
    return state


def clear_create_img_error(bot: Any) -> None:
    state = _provider_create_img_error_state(bot)
    if state is not None:
        state.details = None


def set_create_img_error(bot: Any, details: Dict[str, Any], decision=None) -> None:
    stored = dict(details or {})
    if decision is not None:
        stored.update({
            "error_taxonomy": decision.taxonomy,
            "retryable": decision.retryable,
            "retry_attempt": decision.attempt,
            "retry_attempts": decision.attempt,
            "max_retries": decision.max_retries,
            "retry_exhausted": decision.retryable and not decision.should_retry,
        })
        if decision.retry_after_seconds is not None:
            stored["retry_after_seconds"] = decision.retry_after_seconds
    state = _provider_create_img_error_state(bot)
    if state is not None:
        state.details = stored


def image_error_from_response(response: Any) -> ModelImageCallError:
    return ModelImageCallError(http_error_response(response))


def _status_from_exception_text(exc: BaseException, default_status: int) -> int:
    text = "{} {}".format(type(exc).__name__, exc).lower()
    if "timeout" in text or "timed out" in text:
        return 504
    if any(marker in text for marker in ("connection", "network", "dns", "ssl")):
        return 503
    return default_status


def image_error_details_from_exception(
    exc: BaseException,
    *,
    default_status: int = 500,
    normalizer: Optional[Callable[[BaseException], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if isinstance(exc, ModelImageCallError):
        return dict(exc.details)
    if normalizer is not None:
        try:
            normalized = normalizer(exc)
            if isinstance(normalized, dict):
                return normalized
        except Exception:
            logger.exception("[IMAGE] provider image error normalizer failed")

    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) not in (None, ""):
        return http_error_response(response)

    payload = {}
    for key in (
        "message",
        "code",
        "error_code",
        "type",
        "error_type",
        "status_code",
        "http_code",
        "status",
        "retry_after",
        "retry_after_seconds",
        "retry_after_ms",
    ):
        value = getattr(exc, key, None)
        if value not in (None, ""):
            payload[key] = value

    body = getattr(exc, "body", None) or getattr(exc, "data", None) or getattr(exc, "error", None)
    if isinstance(body, dict):
        nested = body.get("error")
        payload.update({key: value for key, value in body.items() if value not in (None, "")})
        if isinstance(nested, dict):
            payload.update(nested)

    status_code = (
        payload.get("status_code")
        or payload.get("http_code")
        or payload.get("status")
        or getattr(exc, "status_code", None)
        or getattr(exc, "http_status", None)
        or _status_from_exception_text(exc, default_status)
    )
    retry_after = None
    headers = getattr(exc, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if callable(getter):
        retry_after = getter("Retry-After") or getter("retry-after")

    return provider_error_response(
        payload or None,
        message=str(exc),
        status_code=status_code,
        retry_after=retry_after,
        retry_after_seconds=payload.get("retry_after_seconds"),
        retry_after_ms=payload.get("retry_after_ms"),
    )


def create_img_with_retry(
    bot: Any,
    invoke: Callable[[], str],
    *,
    provider: str,
    model: str,
    retry_count: Any = 0,
    max_model_retries: Any = None,
    retry_sleep: Optional[Callable[[float], None]] = None,
    failure_message: str = "Image generation failed, please try again later.",
    error_normalizer: Optional[Callable[[BaseException], Dict[str, Any]]] = None,
) -> tuple:
    """Run a legacy image call through the shared retry policy."""
    clear_create_img_error(bot)
    max_retries = coerce_max_retries(max_model_retries, default=1)
    try:
        attempt = max(0, int(retry_count or 0))
    except (TypeError, ValueError):
        attempt = 0

    while True:
        try:
            image_url = invoke()
            clear_create_img_error(bot)
            return True, image_url
        except Exception as exc:
            details = image_error_details_from_exception(
                exc,
                normalizer=error_normalizer,
            )
            decision = build_retry_decision(
                details,
                attempt=attempt,
                max_retries=max_retries,
            )
            set_create_img_error(bot, details, decision)
            if decision.should_retry:
                logger.warning(
                    "[%s] retrying image generation after %.3fs "
                    "(model=%s attempt=%s/%s taxonomy=%s)",
                    provider.upper(),
                    decision.delay_seconds,
                    model,
                    attempt + 1,
                    max_retries,
                    decision.taxonomy,
                )
                sleep_for_retry(decision.delay_seconds, retry_sleep)
                attempt += 1
                continue
            logger.warning(
                "[%s] image generation failed without retry: %s",
                provider.upper(),
                details.get("message") or exc,
            )
            return False, failure_message

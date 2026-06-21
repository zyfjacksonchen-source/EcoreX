# encoding:utf-8
"""Shared retry evidence helpers for legacy OpenAI reply_text paths."""

from __future__ import annotations

from typing import Any, Dict

from config import conf
from models.model_provider_errors import provider_error_response
from models.model_retry import (
    annotate_retry_evidence,
    build_retry_decision,
    coerce_max_retries,
    sleep_for_retry,
)
from models.openai.openai_compat import (
    APIConnectionError,
    APIError,
    RateLimitError,
    Timeout,
)


def _status_from_exception(exc: BaseException, *, default_status: int = 500) -> int:
    status = getattr(exc, "status_code", None)
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = None
    if status_int is not None:
        return status_int
    if isinstance(exc, RateLimitError):
        return 429
    if isinstance(exc, Timeout):
        return 408
    if isinstance(exc, APIConnectionError):
        return 0
    if isinstance(exc, APIError):
        return 500
    return default_status


def _headers_retry_after(exc: BaseException) -> Any:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after not in (None, ""):
        return retry_after
    headers = getattr(exc, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter("Retry-After") or getter("retry-after")
    return None


def openai_legacy_error_details(
    exc: BaseException,
    *,
    default_status: int = 500,
) -> Dict[str, Any]:
    """Normalize legacy OpenAI/ChatGPT exceptions into telemetry-ready details."""
    body = getattr(exc, "body", None)
    payload: Dict[str, Any] = {}
    if isinstance(body, dict):
        nested_error = body.get("error")
        if isinstance(nested_error, dict):
            payload.update(nested_error)
        elif nested_error not in (None, "", False, True):
            payload["message"] = str(nested_error)
        for key in (
            "status_code",
            "http_code",
            "status",
            "retry_after",
            "retry_after_seconds",
            "retry_after_ms",
        ):
            if body.get(key) not in (None, ""):
                payload.setdefault(key, body.get(key))

    message = (
        payload.get("message")
        or getattr(exc, "message", None)
        or str(exc)
    )
    return provider_error_response(
        payload,
        message=message,
        status_code=_status_from_exception(exc, default_status=default_status),
        retry_after=_headers_retry_after(exc),
    )


def legacy_adapter_error_details(exc: BaseException) -> Dict[str, Any]:
    """Normalize local adapter/runtime exceptions as non-provider failures."""
    message = str(exc) or type(exc).__name__
    return provider_error_response(
        {
            "message": message,
            "code": "",
            "type": "legacy_adapter_error",
        },
        message=message,
        status_code=None,
    )


def legacy_reply_max_retries(default: int = 2) -> int:
    """Read a bounded legacy retry count while preserving the old default of 2."""
    cfg = conf()
    return coerce_max_retries(
        cfg.get("model_max_retries", cfg.get("max_model_retries", default)),
        default=default,
    )


def legacy_reply_failure_result(
    *,
    content: str,
    details: Dict[str, Any],
    decision,
) -> Dict[str, Any]:
    """Build the old reply_text failure shape plus typed retry evidence."""
    annotated = annotate_retry_evidence(details, decision)
    error_value = annotated.get("error")
    error_payload = error_value if isinstance(error_value, dict) else {}
    return {
        "total_tokens": 0,
        "completion_tokens": 0,
        "content": content,
        "message": annotated.get("message") or content,
        "error": error_value or True,
        "status_code": annotated.get("status_code"),
        "error_code": error_payload.get("code") or annotated.get("error_code", ""),
        "error_type": error_payload.get("type") or annotated.get("error_type", ""),
        "error_taxonomy": annotated.get("error_taxonomy"),
        "retry_after": annotated.get("retry_after"),
        "retry_after_seconds": annotated.get("retry_after_seconds"),
        "retry_after_ms": annotated.get("retry_after_ms"),
        "retryable": annotated.get("retryable"),
        "retry_attempt": annotated.get("retry_attempt"),
        "retry_attempts": annotated.get("retry_attempts"),
        "max_retries": annotated.get("max_retries"),
        "retry_exhausted": annotated.get("retry_exhausted"),
    }


def run_legacy_reply_retry_sleep(decision, sleep_fn=None) -> None:
    sleep_for_retry(decision.delay_seconds, sleep_fn)


__all__ = [
    "build_retry_decision",
    "legacy_adapter_error_details",
    "legacy_reply_failure_result",
    "legacy_reply_max_retries",
    "openai_legacy_error_details",
    "run_legacy_reply_retry_sleep",
]

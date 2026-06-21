# encoding:utf-8
"""Shared retry evidence helpers for legacy direct-chat reply_text paths."""

from __future__ import annotations

from typing import Any, Dict

from config import conf
from models.model_provider_errors import http_error_response, provider_error_response
from models.model_retry import (
    annotate_retry_evidence,
    build_retry_decision,
    coerce_max_retries,
    sleep_for_retry,
)

try:
    import requests
except Exception:  # pragma: no cover - requests is a runtime dependency here.
    requests = None


def legacy_direct_chat_max_retries(default: int = 2) -> int:
    cfg = conf()
    return coerce_max_retries(
        cfg.get("model_max_retries", cfg.get("max_model_retries", default)),
        default=default,
    )


def legacy_direct_chat_response_details(response: Any) -> Dict[str, Any]:
    return http_error_response(response)


def legacy_direct_chat_exception_details(exc: BaseException) -> Dict[str, Any]:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) not in (None, ""):
        return http_error_response(response)

    if requests is not None:
        if isinstance(exc, getattr(requests.exceptions, "Timeout")):
            return provider_error_response(
                {"message": str(exc) or "request timeout", "type": "timeout"},
                message=str(exc) or "request timeout",
                status_code=408,
            )
        if isinstance(exc, getattr(requests.exceptions, "ConnectionError")):
            return provider_error_response(
                {"message": str(exc) or "connection error", "type": "network_error"},
                message=str(exc) or "connection error",
                status_code=0,
            )
        if isinstance(exc, getattr(requests.exceptions, "RequestException")):
            return provider_error_response(
                {"message": str(exc) or type(exc).__name__, "type": "network_error"},
                message=str(exc) or type(exc).__name__,
                status_code=0,
            )

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


def legacy_direct_chat_decision(
    details: Dict[str, Any],
    *,
    retry_count: int,
    max_retries: Optional[int] = None,
):
    return build_retry_decision(
        details,
        attempt=retry_count,
        max_retries=legacy_direct_chat_max_retries() if max_retries is None else max_retries,
    )


def legacy_direct_chat_failure_result(
    *,
    content: str,
    details: Dict[str, Any],
    decision,
) -> Dict[str, Any]:
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


def run_legacy_direct_chat_retry_sleep(decision, sleep_fn=None) -> None:
    sleep_for_retry(decision.delay_seconds, sleep_fn)

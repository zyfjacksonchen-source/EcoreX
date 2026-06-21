# encoding:utf-8
"""Shared retry evidence helpers for legacy direct-chat reply_text paths."""

from __future__ import annotations

from typing import Any, Dict, Optional

from config import conf
from models.model_provider_errors import (
    http_error_response,
    provider_error_response,
    response_retry_after,
)
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


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except (AttributeError, KeyError, TypeError):
        return default


def _safe_getitem(obj: Any, key: str, default: Any = None) -> Any:
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return default


def _safe_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if v not in (None, "")}
    keys = _safe_getattr(obj, "keys")
    if callable(keys):
        try:
            return {
                k: _safe_getitem(obj, k)
                for k in keys()
                if _safe_getitem(obj, k) not in (None, "")
            }
        except Exception:
            return {}
    return {}


def _first_present(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _merge_error_payload(payload: Dict[str, Any], source: Any) -> Dict[str, Any]:
    data = _safe_mapping(source)
    for key in (
        "message",
        "msg",
        "code",
        "error_code",
        "type",
        "error_type",
        "status_code",
        "http_code",
        "http_status",
        "status",
        "retry_after",
        "retry_after_seconds",
        "retry_after_ms",
    ):
        value = data.get(key)
        if value not in (None, "") and key not in payload:
            payload[key] = value

    for key in (
        "message",
        "msg",
        "code",
        "error_code",
        "type",
        "error_type",
        "status_code",
        "http_code",
        "http_status",
        "status",
        "retry_after",
        "retry_after_seconds",
        "retry_after_ms",
    ):
        value = _safe_getattr(source, key)
        if value not in (None, "") and key not in payload:
            payload[key] = value

    for container_key in ("body", "data", "error"):
        nested = data.get(container_key)
        if nested in (None, ""):
            nested = _safe_getattr(source, container_key)
        if isinstance(nested, dict):
            nested_error = nested.get("error")
            _merge_error_payload(payload, nested)
            if isinstance(nested_error, dict):
                _merge_error_payload(payload, nested_error)
            elif nested_error not in (None, "", False, True) and "message" not in payload:
                payload["message"] = str(nested_error)
    return payload


def _coerce_provider_status(status_code: Any, default_status: Any) -> Any:
    return status_code if status_code not in (None, "") else default_status


def legacy_direct_chat_sdk_response_details(
    response: Any,
    *,
    message: str = "",
    default_status: Any = 500,
) -> Dict[str, Any]:
    if callable(_safe_getattr(response, "json")) or _safe_getattr(response, "text") not in (None, ""):
        try:
            details = http_error_response(response)
            if message and not details.get("message"):
                details["message"] = message
            for meta_key in ("request_id", "id"):
                meta_value = _safe_getattr(response, meta_key)
                if meta_value not in (None, ""):
                    details[meta_key] = meta_value
            return details
        except Exception:
            pass

    payload = _merge_error_payload({}, response)
    status_code = _coerce_provider_status(
        _first_present(payload, "status_code", "http_code", "http_status", "status"),
        default_status,
    )
    try:
        retry_after = response_retry_after(response)
    except (AttributeError, KeyError, TypeError):
        retry_after = None
    if retry_after in (None, ""):
        headers = _safe_getattr(response, "headers") or {}
        getter = getattr(headers, "get", None)
        if callable(getter):
            retry_after = getter("Retry-After") or getter("retry-after")
    details = provider_error_response(
        payload or None,
        message=message or str(_first_present(payload, "message", "msg") or ""),
        status_code=status_code,
        retry_after=retry_after,
        retry_after_seconds=payload.get("retry_after_seconds"),
        retry_after_ms=payload.get("retry_after_ms"),
    )
    for meta_key in ("request_id", "id"):
        meta_value = _safe_getattr(response, meta_key)
        if meta_value in (None, ""):
            meta_value = _safe_mapping(response).get(meta_key)
        if meta_value not in (None, ""):
            details[meta_key] = meta_value
    return details


def _message_status_hint(exc: BaseException, message: str) -> Any:
    text = "{} {}".format(type(exc).__name__, message).lower()
    if "rate" in text and "limit" in text:
        return 429
    if "timeout" in text or "timed out" in text:
        return 408
    if (
        "connection" in text
        or "network" in text
        or "reset by peer" in text
        or "dns" in text
        or "ssl" in text
    ):
        return 0
    if (
        "gateway" in text
        or "unavailable" in text
        or "server" in text
        or "internal" in text
        or "api error" in text
    ):
        return 500
    if "badrequest" in text or "invalid_request" in text:
        return 400
    return None


def legacy_direct_chat_sdk_exception_details(
    exc: BaseException,
    *,
    default_status: Any = 500,
) -> Dict[str, Any]:
    response = _safe_getattr(exc, "response")
    if response is not None:
        return legacy_direct_chat_sdk_response_details(
            response,
            message=str(exc),
            default_status=default_status,
        )

    message = str(exc) or type(exc).__name__
    if isinstance(exc, TimeoutError):
        return provider_error_response(
            {"message": message, "type": "timeout"},
            message=message,
            status_code=408,
        )
    if isinstance(exc, ConnectionError):
        return provider_error_response(
            {"message": message, "type": "network_error"},
            message=message,
            status_code=0,
        )

    payload = _merge_error_payload({}, exc)
    class_name = type(exc).__name__.lower()
    provider_class_hint = any(
        marker in class_name
        for marker in (
            "api",
            "http",
            "sdk",
            "provider",
            "dashscope",
            "zhipu",
            "rate",
            "timeout",
        )
    )
    status_hint = _message_status_hint(exc, message) if provider_class_hint else None
    provider_like = bool(
        payload
        or provider_class_hint
    )
    if not provider_like:
        return legacy_direct_chat_exception_details(exc)

    status_code = _coerce_provider_status(
        _first_present(payload, "status_code", "http_code", "http_status", "status"),
        status_hint if status_hint is not None else default_status,
    )
    headers = _safe_getattr(exc, "headers") or {}
    retry_after = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        retry_after = getter("Retry-After") or getter("retry-after")
    return provider_error_response(
        payload or None,
        message=message,
        status_code=status_code,
        retry_after=retry_after,
        retry_after_seconds=payload.get("retry_after_seconds"),
        retry_after_ms=payload.get("retry_after_ms"),
    )


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

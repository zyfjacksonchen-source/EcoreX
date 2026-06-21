# encoding:utf-8
"""Shared provider error normalization for native HTTP model adapters."""

from __future__ import annotations

from typing import Any, Dict, Optional


def _first_present(data: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _coerce_status_code(value: Any, default: Any) -> Any:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return default
    if 100 <= status <= 599:
        return status
    return default


def _first_status_code(default: Any, *values: Any) -> Any:
    for value in values:
        coerced = _coerce_status_code(value, None)
        if coerced is not None:
            return coerced
    return _coerce_status_code(default, default)


def _prefer_present(primary: Any, fallback: Any) -> Any:
    if primary not in (None, ""):
        return primary
    return fallback


def response_retry_after(response: Any) -> Optional[Any]:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    return getter("Retry-After") or getter("retry-after")


def provider_error_response(
    error: Any = None,
    *,
    message: Any = "",
    status_code: Any = 500,
    retry_after: Any = None,
    retry_after_seconds: Any = None,
    retry_after_ms: Any = None,
) -> Dict[str, Any]:
    error_message = message
    error_code = ""
    error_type = ""
    status_code = _coerce_status_code(status_code, status_code)
    if isinstance(error, dict):
        error_message = _first_present(error, "message", "msg") or error_message
        error_code = _first_present(error, "code", "error_code") or ""
        error_type = _first_present(error, "type", "error_type") or ""
        status_code = _first_status_code(
            status_code,
            error.get("status_code"),
            error.get("http_code"),
            error.get("status"),
        )
        retry_after = _prefer_present(
            _first_present(error, "retry_after"),
            retry_after,
        )
        retry_after_seconds = _prefer_present(
            _first_present(error, "retry_after_seconds"),
            retry_after_seconds,
        )
        retry_after_ms = _prefer_present(
            _first_present(error, "retry_after_ms"),
            retry_after_ms,
        )
    elif error not in (None, False, True):
        error_message = error_message or str(error)

    error_message = str(error_message or "")
    status_code = _coerce_status_code(status_code, status_code)
    response = {
        "error": {
            "message": error_message,
            "code": str(error_code or ""),
            "type": str(error_type or ""),
        },
        "message": error_message,
        "status_code": status_code,
    }
    if retry_after not in (None, ""):
        response["retry_after"] = retry_after
    if retry_after_seconds not in (None, ""):
        response["retry_after_seconds"] = retry_after_seconds
    if retry_after_ms not in (None, ""):
        response["retry_after_ms"] = retry_after_ms
    return response


def http_error_response(response: Any) -> Dict[str, Any]:
    status_code = getattr(response, "status_code", None)
    data: Any = {}
    try:
        loaded = response.json()
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}

    error = data.get("error") if isinstance(data, dict) else None
    error_payload: Any = error
    if isinstance(data, dict):
        metadata = {}
        for key in (
            "code",
            "error_code",
            "type",
            "error_type",
            "status_code",
            "status",
            "http_code",
            "retry_after",
            "retry_after_seconds",
            "retry_after_ms",
        ):
            if data.get(key) not in (None, ""):
                metadata[key] = data.get(key)
        if isinstance(error, dict):
            metadata.update(error)
        elif error not in (None, False, True):
            metadata.setdefault("message", str(error))
        if metadata:
            error_payload = metadata

    message = ""
    if isinstance(data, dict):
        message = data.get("message") or data.get("msg") or ""
    if not message and error not in (None, False, True):
        message = error.get("message") if isinstance(error, dict) else str(error)
    if not message:
        message = str(getattr(response, "text", "") or "")
    if not message:
        message = "Provider request failed with HTTP {}".format(status_code)

    return provider_error_response(
        error_payload,
        message=message,
        status_code=status_code,
        retry_after=response_retry_after(response),
    )

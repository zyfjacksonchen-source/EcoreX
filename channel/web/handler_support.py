"""Shared helpers for Web route handlers.

This module is intentionally small while S8 peels handlers out of
``web_channel.py``. Auth and workspace lookups bridge back to the legacy
module so existing tests and monkeypatches keep working during the split.
"""

import hashlib
from typing import Any, Dict


def require_auth() -> None:
    from channel.web import web_channel

    web_channel._require_auth()


def get_workspace_root() -> str:
    from channel.web import web_channel

    return web_channel._get_workspace_root()


def web_body_log_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "redacted": bool(text),
        "hash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] if text else "",
        "chars": len(text),
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


def public_exception_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "errorType": type(value).__name__ if value is not None else "",
        "errorHash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16] if text else "",
        "errorLength": len(text),
        "errorBytes": len(text.encode("utf-8", errors="replace")),
    }


def public_exception_message(prefix: str, value: Any) -> str:
    summary = public_exception_summary(value)
    if not summary["errorHash"]:
        return prefix
    return (
        f"{prefix} Details redacted "
        f"(type={summary['errorType']}, hash={summary['errorHash']}, "
        f"chars={summary['errorLength']}, bytes={summary['errorBytes']})."
    )


def public_error_payload(prefix: str, value: Any, **extra: Any) -> Dict[str, Any]:
    return {
        "status": "error",
        "message": public_exception_message(prefix, value),
        **public_exception_summary(value),
        **extra,
    }

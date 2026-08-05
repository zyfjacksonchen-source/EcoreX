"""Process-local projection from verified Runtime permissions to legacy tools."""

from __future__ import annotations

import threading


_LOCK = threading.Lock()
_FULL_ACCESS: bool | None = None


def sync_verified_runtime_permission(*, full_access: bool) -> None:
    global _FULL_ACCESS
    with _LOCK:
        _FULL_ACCESS = bool(full_access)


def verified_runtime_permission() -> bool | None:
    with _LOCK:
        return _FULL_ACCESS


def verified_runtime_full_access() -> bool:
    return verified_runtime_permission() is True


__all__ = [
    "sync_verified_runtime_permission",
    "verified_runtime_full_access",
    "verified_runtime_permission",
]

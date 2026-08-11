"""Optional e-Mate Agent bridge binding for native Cow channels."""

from __future__ import annotations

from threading import Lock
from typing import Any


_bridge: Any | None = None
_lock = Lock()


def bind_runtime_bridge(bridge: Any) -> None:
    global _bridge
    with _lock:
        _bridge = bridge


def unbind_runtime_bridge(bridge: Any) -> None:
    global _bridge
    with _lock:
        if _bridge is bridge:
            _bridge = None


def current_runtime_bridge() -> Any | None:
    with _lock:
        return _bridge


__all__ = ["bind_runtime_bridge", "current_runtime_bridge", "unbind_runtime_bridge"]

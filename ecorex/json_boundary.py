"""Small iterative limits for JSON decoded at remote trust boundaries."""

from __future__ import annotations

from typing import Any


class JSONComplexityError(ValueError):
    """A decoded JSON value exceeds the accepted structural envelope."""


def validate_json_complexity(
    value: Any,
    *,
    max_depth: int = 32,
    max_nodes: int = 50_000,
) -> None:
    if max_depth < 0 or max_nodes < 1:
        raise ValueError("JSON complexity limits are invalid")
    remaining = max_nodes
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise JSONComplexityError("JSON contains too many values")
        if depth > max_depth:
            raise JSONComplexityError("JSON is nested too deeply")
        if current is None or isinstance(current, (bool, int, float, str)):
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise JSONComplexityError("JSON object keys must be strings")
            pending.extend((item, depth + 1) for item in current.values())
            continue
        raise JSONComplexityError("value is not JSON-compatible")


__all__ = ["JSONComplexityError", "validate_json_complexity"]

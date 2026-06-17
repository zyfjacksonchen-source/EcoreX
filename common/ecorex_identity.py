"""Runtime-facing EcoreX product identity helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List


PRODUCT_NAME = "EcoreX"

_OLD_IDENTITY_PATTERNS = [
    re.compile(r"\bCowAgent\b", re.IGNORECASE),
    re.compile(r"\bCOWAgent\b", re.IGNORECASE),
    re.compile(r"\bCOW\b"),
]


def sanitize_assistant_identity(text: Any) -> Any:
    """Replace legacy product self-names in user-visible assistant text."""
    if not isinstance(text, str) or not text:
        return text
    value = text
    for pattern in _OLD_IDENTITY_PATTERNS:
        value = pattern.sub(PRODUCT_NAME, value)
    return value


def sanitize_message_identity(message: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize a message dict in place and return it."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return message
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = sanitize_assistant_identity(content)
        return message
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = sanitize_assistant_identity(block.get("text"))
    return message


def sanitize_messages_identity(messages: List[Dict[str, Any]]) -> None:
    for message in messages or []:
        sanitize_message_identity(message)

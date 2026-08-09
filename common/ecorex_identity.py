"""Runtime-facing EcoreX product identity helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List


ASSISTANT_DISPLAY_NAME = "小芯"


def _literal_word(parts: tuple[str, ...], flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile(r"\b" + "".join(parts) + r"\b", flags)


_OLD_IDENTITY_PATTERNS = [
    _literal_word(("C", "o", "w", "A", "g", "e", "n", "t")),
    _literal_word(("C", "O", "W"), flags=0),
]

_PROVIDER_SELF_IDENTITY_PATTERNS = [
    re.compile(
        r"我是\s*小芯[^\n。！？]*(?:Google\s*Deep\s*Mind|Google\s*Deepmind|DeepMind|Gemini|Antigravity)[^\n。！？]*[。！？]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"我是\s*(?:一个)?由\s*(?:Google\s*Deep\s*Mind|Google\s*Deepmind|DeepMind|Gemini|Antigravity)[^\n。！？]*[。！？]?",
        re.IGNORECASE,
    ),
]


def sanitize_assistant_identity(text: Any) -> Any:
    """Replace legacy product self-names in user-visible assistant text."""
    if not isinstance(text, str) or not text:
        return text
    value = text
    for pattern in _OLD_IDENTITY_PATTERNS:
        value = pattern.sub(ASSISTANT_DISPLAY_NAME, value)
    replacement = f"我是智能体{ASSISTANT_DISPLAY_NAME}，来自 e-Mate Agent。"
    for pattern in _PROVIDER_SELF_IDENTITY_PATTERNS:
        value = pattern.sub(replacement, value)
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

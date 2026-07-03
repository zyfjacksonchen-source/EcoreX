"""Public payload redaction helpers for UI-facing runtime disclosures."""

from __future__ import annotations

import re
import json
from typing import Any, Dict, List


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?(?:key|base)|base[_-]?url|endpoint|token|password|passwd|secret|authorization|cookie|session|device[_-]?code|app[_-]?id|client[_-]?id|open[_-]?id|chat[_-]?id|union[_-]?id|message[_-]?id|receive[_-]?id|qrcode|qr[_-]?(?:url|image)|verification[_-]?(?:url|uri)|home[_-]?channel)"
)
_CONTENT_KEY_RE = re.compile(
    r"(?i)^(content|contents|body|file_content|file_contents|source|script|code|markdown|prompt|instructions?)$"
)
_SENSITIVE_TEXT_PATTERNS = [
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{12,}"), "github_pat_***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"), "ghp_***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"), "xox-***"),
    (re.compile(r"xapp-[A-Za-z0-9\-]{8,}"), "xapp-***"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{20,}\b"), "telegram-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"\bcli_[A-Za-z0-9_-]{8,}\b"), "cli_***"),
    (re.compile(r"\b(?:ou|oc|om)_[A-Za-z0-9_-]{8,}\b"), "feishu-id-***"),
    (re.compile(r"\b(?:file|img)_v\d+_[A-Za-z0-9_-]{8,}\b"), "feishu-resource-***"),
    (re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+(?:\\[^\s\"')<>]+)*"), r"C:\\Users\\[redacted]"),
    (re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\"')<>]+\\)*[^\s\"')<>]+"), r"C:\\[redacted-path]"),
    (re.compile(r"(?i)\b/(?:Users|home|tmp|var|private)/[^\s\"')<>]+"), "/[redacted-path]"),
    (re.compile(r"(?i)https://open\.feishu\.cn/[^\s\"')<>]+"), "https://open.feishu.cn/[redacted]"),
    (re.compile(r"(?i)data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]{32,}"), "data:image/[redacted]"),
    (
        re.compile(r"(?i)(api[_-]?(?:key|base)|base[_-]?url|endpoint|token|password|passwd|secret|authorization|cookie|device[_-]?code)(\"?\s*[:=]\s*\"?)[^\",\s&}]+"),
        r"\1\2***",
    ),
]


def mask_sensitive_text(value: Any, *, max_chars: int = 512) -> str:
    text = str(value or "")
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n...[redacted {len(text) - max_chars} chars]"
    return text


def redact_public_tool_value(value: Any, *, max_depth: int = 5, max_items: int = 20, max_chars: int = 512) -> Any:
    """Return a bounded, UI-safe view of tool input/output metadata.

    This helper preserves structural hints (keys, filenames, scalar flags) while
    removing raw file/prompt bodies and common credentials before the payload is
    sent to browser clients or local UI storage.
    """

    def _redact(item: Any, depth: int, parent_key: str = "") -> Any:
        lowered_parent = str(parent_key or "").lower()
        if _SENSITIVE_KEY_RE.search(lowered_parent):
            return "[redacted]"
        if _CONTENT_KEY_RE.match(lowered_parent):
            return "[redacted-content]"
        if depth <= 0:
            return "[redacted-nested]"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                    return _redact(parsed, depth - 1, parent_key)
                except Exception:
                    pass
            return mask_sensitive_text(item, max_chars=max_chars)
        if isinstance(item, dict):
            safe: Dict[str, Any] = {}
            try:
                total_items = len(item)
            except Exception:
                total_items = None
            for index, (key, child) in enumerate(item.items()):
                if index >= max_items:
                    safe["redacted_omitted_field_count"] = (
                        max(1, total_items - max_items)
                        if isinstance(total_items, int)
                        else 1
                    )
                    break
                normalized = str(key or "")
                if _SENSITIVE_KEY_RE.search(normalized):
                    safe[normalized] = "[redacted]"
                    continue
                if _CONTENT_KEY_RE.match(normalized):
                    safe[normalized] = "[redacted-content]"
                    continue
                safe[normalized] = _redact(child, depth - 1, normalized)
            return safe
        if isinstance(item, (list, tuple)):
            safe_list: List[Any] = [
                _redact(child, depth - 1, parent_key)
                for child in list(item)[:max_items]
            ]
            if len(item) > max_items:
                safe_list.append({"redacted_omitted_item_count": len(item) - max_items})
            return safe_list
        return mask_sensitive_text(item, max_chars=max_chars)

    return _redact(value, max_depth)

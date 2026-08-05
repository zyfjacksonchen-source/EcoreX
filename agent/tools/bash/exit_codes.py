"""Interpret informational non-zero shell exits without hiding real errors."""

from typing import Optional, Tuple


_SOFT_EXIT_1 = {
    "grep": "No matches found",
    "egrep": "No matches found",
    "fgrep": "No matches found",
    "rg": "No matches found",
    "ag": "No matches found",
    "find": "Some directories were inaccessible",
    "diff": "Files differ",
    "cmp": "Files differ",
    "test": "Condition is false",
    "[": "Condition is false",
}
_SEPARATORS = (";", "&&", "||", "|", "\n")


def _last_segment(command: str) -> str:
    segment_start = 0
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        for separator in _SEPARATORS:
            if command.startswith(separator, index):
                segment_start = index + len(separator)
                index += len(separator)
                break
        else:
            index += 1
    return command[segment_start:]


def _base_command(segment: str) -> str:
    for token in segment.strip().lstrip("({ \t").split():
        if "=" in token and not token.startswith("="):
            continue
        return token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return ""


def interpret(command: str, exit_code: int) -> Tuple[bool, Optional[str]]:
    """Return ``(is_error, note)`` for a shell command exit."""
    if exit_code == 0:
        return False, None
    if exit_code == 1:
        meaning = _SOFT_EXIT_1.get(_base_command(_last_segment(command)))
        if meaning:
            return False, meaning
    return True, None

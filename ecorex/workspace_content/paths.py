"""Portable relative-path contract shared by knowledge import and Runtime."""

from __future__ import annotations

from pathlib import PurePosixPath
import unicodedata


MAX_PATH_LENGTH = 1_024
MAX_SEGMENT_LENGTH = 128
MAX_DEPTH = 16
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_WINDOWS_DEVICES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{value}" for value in range(1, 10)),
        *(f"lpt{value}" for value in range(1, 10)),
    }
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


def normalize_knowledge_path(value: str, *, allow_root: bool = False) -> PurePosixPath:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise ValueError("knowledge path is invalid")
    if not raw:
        if allow_root:
            return PurePosixPath()
        raise ValueError("knowledge path is required")
    path = PurePosixPath(raw)
    if (
        len(raw) > MAX_PATH_LENGTH
        or path.is_absolute()
        or len(path.parts) > MAX_DEPTH
        or any(
            part in {"", ".", ".."}
            or part.startswith(".")
            or part.endswith((".", " "))
            or len(part) > MAX_SEGMENT_LENGTH
            or any(unicodedata.category(character).startswith("C") for character in part)
            or any(character in _WINDOWS_FORBIDDEN for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICES
            for part in path.parts
        )
    ):
        raise ValueError("knowledge path is invalid")
    return path


__all__ = [
    "MAX_DEPTH",
    "MAX_DOCUMENT_BYTES",
    "MAX_PATH_LENGTH",
    "MAX_SEGMENT_LENGTH",
    "normalize_knowledge_path",
]

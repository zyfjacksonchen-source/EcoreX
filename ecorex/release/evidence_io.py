"""Fail-closed reads for immutable CI evidence and release identity files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any


def read_stable_regular_file(
    value: str | os.PathLike[str],
    *,
    maximum_bytes: int,
    code: str,
) -> bytes:
    """Read one bounded file while rejecting links, reparses and TOCTOU drift."""

    if maximum_bytes < 1:
        raise ValueError(code)
    try:
        raw = Path(value).expanduser()
        absolute = Path(os.path.abspath(raw))
        _reject_link_components(absolute, code=code)
        before = absolute.lstat()
        if not _regular(before) or not 1 <= before.st_size <= maximum_bytes:
            raise ValueError(code)
        chunks: list[bytes] = []
        total = 0
        with absolute.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or not _regular(opened):
                raise ValueError(code)
            while chunk := stream.read(min(1024 * 1024, maximum_bytes + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    raise ValueError(code)
            after = os.fstat(stream.fileno())
        current = absolute.lstat()
    except ValueError:
        raise
    except (OSError, TypeError):
        raise ValueError(code) from None
    identity = _identity(before)
    if (
        not 1 <= total <= maximum_bytes
        or _identity(opened) != identity
        or _identity(after) != identity
        or _identity(current) != identity
        or not _regular(current)
    ):
        raise ValueError(code)
    return b"".join(chunks)


def strict_json_loads(payload: bytes, *, code: str) -> Any:
    """Decode UTF-8 JSON while refusing JavaScript-only non-finite numbers."""

    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError(code)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError(code) from None


def write_new_json_file(
    value: Any,
    path: str | os.PathLike[str],
    *,
    code: str,
) -> Path:
    """Create one canonical JSON receipt without an exists/write race."""

    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        output = Path(path).expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return output
    except (OSError, TypeError, ValueError, RecursionError):
        raise ValueError(code) from None


def _reject_link_components(path: Path, *, code: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        metadata = current.lstat()
        if _linked(metadata):
            raise ValueError(code)


def _linked(value: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse
    )


def _regular(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and not _linked(value)


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


__all__ = ["read_stable_regular_file", "strict_json_loads", "write_new_json_file"]

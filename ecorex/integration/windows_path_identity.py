"""Windows-invariant path identity shared by Python and the native helper.

Windows path policy must not use Python's Unicode ``casefold`` because it can
expand characters (for example ``ß`` to ``ss``) that Windows ordinal path
comparison keeps distinct. The native helper uses the same invariant
``LCMapStringEx`` lowercase mapping before UTF-8 hashing.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
from os import PathLike


_LCMAP_LOWERCASE = 0x00000100
if os.name == "nt":
    _LC_MAP_STRING_EX = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).LCMapStringEx
    _LC_MAP_STRING_EX.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ssize_t,
    )
    _LC_MAP_STRING_EX.restype = ctypes.c_int
else:
    _LC_MAP_STRING_EX = None


class WindowsPathIdentityError(ValueError):
    pass


def windows_invariant_path_key(value: str | PathLike[str]) -> str:
    """Return one locale-independent Windows path hash/sort key."""

    if os.name != "nt":
        raise WindowsPathIdentityError(
            "Windows path identity is unavailable on this platform"
        )
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise WindowsPathIdentityError("Windows path identity input is invalid")
    normalized = ntpath.normpath(raw)
    while len(normalized) > 3 and normalized.endswith(("\\", "/")):
        normalized = normalized[:-1]
    assert _LC_MAP_STRING_EX is not None
    required = _LC_MAP_STRING_EX(
        "",
        _LCMAP_LOWERCASE,
        normalized,
        len(normalized),
        None,
        0,
        None,
        None,
        0,
    )
    if required <= 0:
        raise WindowsPathIdentityError(
            f"Windows invariant path mapping failed ({ctypes.get_last_error()})"
        )
    buffer = ctypes.create_unicode_buffer(required)
    written = _LC_MAP_STRING_EX(
        "",
        _LCMAP_LOWERCASE,
        normalized,
        len(normalized),
        buffer,
        required,
        None,
        None,
        0,
    )
    if written != required:
        raise WindowsPathIdentityError(
            f"Windows invariant path mapping changed ({ctypes.get_last_error()})"
        )
    return buffer[:written]


__all__ = ["WindowsPathIdentityError", "windows_invariant_path_key"]

"""Small Windows CurrentUser DPAPI boundary used by operator-only secrets."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


_UI_FORBIDDEN = 0x1


class WindowsDPAPIError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def protect_current_user(payload: bytes, *, entropy: bytes, description: str) -> bytes:
    if os.name != "nt" or not payload or not entropy or not description:
        raise WindowsDPAPIError("windows_dpapi_input_invalid")
    crypt32, kernel32 = _libraries()
    source, source_buffer = _blob(payload)
    salt, salt_buffer = _blob(entropy)
    output = _Blob()
    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            description,
            ctypes.byref(salt),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok or not output.pbData or output.cbData < 1:
            raise WindowsDPAPIError("windows_dpapi_protect_failed")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(salt_buffer, 0, len(salt_buffer))
        if output.pbData:
            kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def unprotect_current_user(payload: bytes, *, entropy: bytes) -> bytearray:
    if os.name != "nt" or not payload or not entropy:
        raise WindowsDPAPIError("windows_dpapi_input_invalid")
    crypt32, kernel32 = _libraries()
    source, source_buffer = _blob(payload)
    salt, salt_buffer = _blob(entropy)
    output = _Blob()
    description = wintypes.LPWSTR()
    try:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(salt),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok or not output.pbData or output.cbData < 1:
            raise WindowsDPAPIError("windows_dpapi_unprotect_failed")
        return bytearray(ctypes.string_at(output.pbData, output.cbData))
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(salt_buffer, 0, len(salt_buffer))
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        if output.pbData:
            kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def _blob(value: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _libraries():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


__all__ = [
    "WindowsDPAPIError",
    "protect_current_user",
    "unprotect_current_user",
]

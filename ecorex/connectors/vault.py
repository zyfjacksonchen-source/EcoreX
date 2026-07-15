"""Credential vault boundary backed by the operating-system credential store.

The serialized credential document is never returned in error messages and is
never written to SQLite.  Backend injection is intentionally supported so the
failure semantics can be tested without touching a developer's real keychain.
"""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
from ctypes import wintypes
import json
import re
import sys
import threading
from typing import Protocol


_REFERENCE_RE = re.compile(r"^ecorex/[A-Za-z0-9._/-]{1,1000}$")


class CredentialVault(Protocol):
    def put(self, reference: str, material: Mapping[str, str]) -> None:
        ...

    def get(self, reference: str) -> Mapping[str, str]:
        ...

    def delete(self, reference: str) -> None:
        ...


class BinaryCredentialBackend(Protocol):
    def put(self, reference: str, payload: bytes) -> None:
        ...

    def get(self, reference: str) -> bytes:
        ...

    def delete(self, reference: str) -> None:
        ...


def _validate_reference(reference: str) -> str:
    value = str(reference)
    if not _REFERENCE_RE.fullmatch(value):
        raise ValueError("invalid credential reference")
    return value


def _serialize(material: Mapping[str, str]) -> bytes:
    if not material:
        raise ValueError("credential material is required")
    normalized: dict[str, str] = {}
    for key, value in material.items():
        name = str(key)
        secret = str(value)
        if (
            not name
            or not secret
            or len(name) > 256
            or "\x00" in name
            or "\x00" in secret
        ):
            raise ValueError("credential keys and values must be non-empty text")
        normalized[name] = secret
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("credential material exceeds the vault size limit")
    return encoded


def _deserialize(payload: bytes) -> Mapping[str, str]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("credential vault returned invalid data") from None
    if not isinstance(value, dict) or not value:
        raise RuntimeError("credential vault returned invalid data")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise RuntimeError("credential vault returned invalid data")
    return dict(value)


class SerializedCredentialVault:
    """Map the connector credential contract to an opaque binary backend."""

    def __init__(self, backend: BinaryCredentialBackend) -> None:
        self._backend = backend

    def put(self, reference: str, material: Mapping[str, str]) -> None:
        reference = _validate_reference(reference)
        try:
            payload = bytearray(_serialize(material))
        except Exception:
            raise RuntimeError("credential vault serialization failed") from None
        try:
            self._backend.put(reference, bytes(payload))
        except Exception:
            raise RuntimeError("credential vault write failed") from None
        finally:
            for index in range(len(payload)):
                payload[index] = 0

    def get(self, reference: str) -> Mapping[str, str]:
        reference = _validate_reference(reference)
        try:
            payload = bytearray(self._backend.get(reference))
        except Exception:
            raise RuntimeError("credential vault read failed") from None
        try:
            return _deserialize(bytes(payload))
        finally:
            for index in range(len(payload)):
                payload[index] = 0

    def delete(self, reference: str) -> None:
        reference = _validate_reference(reference)
        try:
            self._backend.delete(reference)
        except Exception:
            raise RuntimeError("credential vault delete failed") from None


class RejectingCredentialVault:
    """Production-safe default until an OS keychain capability is installed."""

    def put(self, reference: str, material: Mapping[str, str]) -> None:
        del reference, material
        raise RuntimeError("OS credential vault capability is not configured")

    def get(self, reference: str) -> Mapping[str, str]:
        del reference
        raise RuntimeError("OS credential vault capability is not configured")

    def delete(self, reference: str) -> None:
        del reference
        raise RuntimeError("OS credential vault capability is not configured")


class InMemoryCredentialVault:
    """Test-only vault. Production composition must use the OS keychain."""

    def __init__(self) -> None:
        self._values: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def put(self, reference: str, material: Mapping[str, str]) -> None:
        reference = _validate_reference(reference)
        if not material:
            raise ValueError("credential reference and material are required")
        with self._lock:
            self._values[reference] = {str(key): str(value) for key, value in material.items()}

    def get(self, reference: str) -> Mapping[str, str]:
        reference = _validate_reference(reference)
        with self._lock:
            if reference not in self._values:
                raise KeyError(reference)
            return dict(self._values[reference])

    def delete(self, reference: str) -> None:
        reference = _validate_reference(reference)
        with self._lock:
            self._values.pop(reference, None)


class _WindowsCredentialBackend:
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Credential Manager is unavailable")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_write = self._advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(self._CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_read = self._advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(self._CREDENTIALW)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_delete = self._advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = self._advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    @staticmethod
    def _target(reference: str) -> str:
        return "EcoreX:" + reference

    def put(self, reference: str, payload: bytes) -> None:
        if len(payload) > 5 * 512:
            raise RuntimeError("credential payload exceeds Windows Credential Manager limit")
        blob = ctypes.create_string_buffer(payload, len(payload))
        credential = self._CREDENTIALW()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = self._target(reference)
        credential.CredentialBlobSize = len(payload)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "EcoreX"
        try:
            if not self._cred_write(ctypes.byref(credential), 0):
                raise OSError(ctypes.get_last_error())
        finally:
            ctypes.memset(ctypes.addressof(blob), 0, len(payload))

    def get(self, reference: str) -> bytes:
        pointer = ctypes.POINTER(self._CREDENTIALW)()
        if not self._cred_read(
            self._target(reference), self._CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            raise KeyError(reference)
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            self._cred_free(pointer)

    def delete(self, reference: str) -> None:
        if self._cred_delete(self._target(reference), self._CRED_TYPE_GENERIC, 0):
            return
        code = ctypes.get_last_error()
        if code != self._ERROR_NOT_FOUND:
            raise OSError(code)


class _MacOSKeychainBackend:
    """Store generic passwords through Security.framework's SecItem API."""

    _SERVICE = "com.ecorex.connector-credentials"
    _ERR_SUCCESS = 0
    _ERR_DUPLICATE_ITEM = -25299
    _ERR_ITEM_NOT_FOUND = -25300
    _CF_STRING_ENCODING_UTF8 = 0x08000100

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("macOS Keychain is unavailable")
        try:
            self._cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            self._security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
        except OSError:
            raise RuntimeError("macOS Keychain framework is unavailable") from None
        self._configure_functions()
        self._k_sec_class = self._constant(self._security, "kSecClass")
        self._k_sec_class_generic_password = self._constant(
            self._security, "kSecClassGenericPassword"
        )
        self._k_sec_attr_service = self._constant(self._security, "kSecAttrService")
        self._k_sec_attr_account = self._constant(self._security, "kSecAttrAccount")
        self._k_sec_value_data = self._constant(self._security, "kSecValueData")
        self._k_sec_return_data = self._constant(self._security, "kSecReturnData")
        self._k_sec_match_limit = self._constant(self._security, "kSecMatchLimit")
        self._k_sec_match_limit_one = self._constant(
            self._security, "kSecMatchLimitOne"
        )
        self._k_sec_attr_accessible = self._constant(
            self._security, "kSecAttrAccessible"
        )
        self._k_sec_accessible = self._constant(
            self._security, "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly"
        )
        self._cf_true = self._constant(self._cf, "kCFBooleanTrue")
        self._dictionary_key_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryKeyCallBacks")
        )
        self._dictionary_value_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryValueCallBacks")
        )

    def put(self, reference: str, payload: bytes) -> None:
        query, owned = self._base_query(reference)
        data = self._cf_data(payload)
        try:
            self._cf.CFDictionarySetValue(query, self._k_sec_value_data, data)
            self._cf.CFDictionarySetValue(
                query, self._k_sec_attr_accessible, self._k_sec_accessible
            )
            status = int(self._security.SecItemAdd(query, None))
            if status == self._ERR_DUPLICATE_ITEM:
                updates = self._dictionary()
                match_query, match_owned = self._base_query(reference)
                try:
                    self._cf.CFDictionarySetValue(
                        updates, self._k_sec_value_data, data
                    )
                    status = int(
                        self._security.SecItemUpdate(match_query, updates)
                    )
                finally:
                    self._cf.CFRelease(updates)
                    self._release_query(match_query, match_owned)
            if status != self._ERR_SUCCESS:
                raise OSError(status)
        finally:
            self._cf.CFRelease(data)
            self._release_query(query, owned)

    def get(self, reference: str) -> bytes:
        query, owned = self._base_query(reference)
        result = ctypes.c_void_p()
        try:
            self._cf.CFDictionarySetValue(query, self._k_sec_return_data, self._cf_true)
            self._cf.CFDictionarySetValue(
                query, self._k_sec_match_limit, self._k_sec_match_limit_one
            )
            status = int(self._security.SecItemCopyMatching(query, ctypes.byref(result)))
            if status == self._ERR_ITEM_NOT_FOUND:
                raise KeyError(reference)
            if status != self._ERR_SUCCESS or not result.value:
                raise OSError(status)
            length = int(self._cf.CFDataGetLength(result))
            pointer = self._cf.CFDataGetBytePtr(result)
            return ctypes.string_at(pointer, length)
        finally:
            if result.value:
                self._cf.CFRelease(result)
            self._release_query(query, owned)

    def delete(self, reference: str) -> None:
        query, owned = self._base_query(reference)
        try:
            status = int(self._security.SecItemDelete(query))
            if status not in {self._ERR_SUCCESS, self._ERR_ITEM_NOT_FOUND}:
                raise OSError(status)
        finally:
            self._release_query(query, owned)

    @staticmethod
    def _constant(library: ctypes.CDLL, name: str) -> ctypes.c_void_p:
        value = ctypes.c_void_p.in_dll(library, name).value
        if value is None:
            raise RuntimeError("macOS Keychain constant is unavailable")
        return ctypes.c_void_p(value)

    def _configure_functions(self) -> None:
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_long,
        ]
        self._cf.CFDataCreate.restype = ctypes.c_void_p
        self._cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetLength.restype = ctypes.c_long
        self._cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        self._cf.CFDictionaryCreateMutable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._cf.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        self._cf.CFDictionarySetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._cf.CFDictionarySetValue.restype = None
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecItemDelete.restype = ctypes.c_int32

    def _dictionary(self) -> ctypes.c_void_p:
        value = self._cf.CFDictionaryCreateMutable(
            None,
            0,
            ctypes.c_void_p(self._dictionary_key_callbacks),
            ctypes.c_void_p(self._dictionary_value_callbacks),
        )
        if not value:
            raise MemoryError("could not allocate Keychain query")
        return ctypes.c_void_p(value)

    def _cf_string(self, value: str) -> ctypes.c_void_p:
        pointer = self._cf.CFStringCreateWithCString(
            None,
            value.encode("utf-8"),
            self._CF_STRING_ENCODING_UTF8,
        )
        if not pointer:
            raise ValueError("could not encode Keychain identifier")
        return ctypes.c_void_p(pointer)

    def _cf_data(self, payload: bytes) -> ctypes.c_void_p:
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        pointer = self._cf.CFDataCreate(None, buffer, len(payload))
        ctypes.memset(ctypes.addressof(buffer), 0, len(payload))
        if not pointer:
            raise MemoryError("could not allocate Keychain value")
        return ctypes.c_void_p(pointer)

    def _base_query(
        self, reference: str
    ) -> tuple[ctypes.c_void_p, tuple[ctypes.c_void_p, ctypes.c_void_p]]:
        query = self._dictionary()
        service = self._cf_string(self._SERVICE)
        account = self._cf_string(reference)
        self._cf.CFDictionarySetValue(
            query, self._k_sec_class, self._k_sec_class_generic_password
        )
        self._cf.CFDictionarySetValue(query, self._k_sec_attr_service, service)
        self._cf.CFDictionarySetValue(query, self._k_sec_attr_account, account)
        return query, (service, account)

    def _release_query(
        self,
        query: ctypes.c_void_p,
        owned: tuple[ctypes.c_void_p, ctypes.c_void_p],
    ) -> None:
        self._cf.CFRelease(query)
        for value in owned:
            self._cf.CFRelease(value)


class WindowsCredentialVault(SerializedCredentialVault):
    def __init__(self, backend: BinaryCredentialBackend | None = None) -> None:
        super().__init__(backend or _WindowsCredentialBackend())


class MacOSKeychainCredentialVault(SerializedCredentialVault):
    def __init__(self, backend: BinaryCredentialBackend | None = None) -> None:
        super().__init__(backend or _MacOSKeychainBackend())


def production_credential_vault() -> CredentialVault:
    """Return the platform vault or fail closed on unsupported platforms."""

    if sys.platform == "win32":
        return WindowsCredentialVault()
    if sys.platform == "darwin":
        return MacOSKeychainCredentialVault()
    raise RuntimeError("no supported production credential vault is available")


__all__ = [
    "BinaryCredentialBackend",
    "CredentialVault",
    "InMemoryCredentialVault",
    "MacOSKeychainCredentialVault",
    "RejectingCredentialVault",
    "SerializedCredentialVault",
    "WindowsCredentialVault",
    "production_credential_vault",
]

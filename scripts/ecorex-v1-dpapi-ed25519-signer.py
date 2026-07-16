#!/usr/bin/env python3
"""Current-user DPAPI Ed25519 adapter for an explicit direct-release waiver.

This adapter is intentionally narrower than the normal KMS/HSM path.  It owns
exactly two persistent Ed25519 seeds under the current Windows user's local
application-data directory:

* ``release`` signs Candidate artifacts, manifests and waiver evidence;
* ``publication`` signs only the public Bootstrap freshness domain.

The private seeds are DPAPI protected at rest, never accepted through argv or
environment variables, and never written to stdout/stderr.  With no command
argument the process implements the stdin-only protocol expected by
``DigestPinnedExternalSigner`` and emits one Base64 signature.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_SCHEMA_VERSION = 1
_ROLES = ("release", "publication")
_MAX_KEY_DOCUMENT_BYTES = 64 * 1024
_MAX_SIGNING_PAYLOAD_BYTES = 16 * 1024 * 1024
_PUBLICATION_DOMAIN = b"ecorex.public-bootstrap-freshness.v1\0"
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("command", nargs="?", choices=("initialize", "describe"))
    return parser


def _key_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not isinstance(local, str) or not local or "\x00" in local:
        raise RuntimeError("direct_release_key_store_unavailable")
    base = Path(local)
    if not base.is_absolute():
        raise RuntimeError("direct_release_key_store_unavailable")
    return base / "EcoreX" / "release-operator" / "v1"


def _entropy(role: str) -> bytes:
    if role not in _ROLES:
        raise RuntimeError("direct_release_key_role_invalid")
    return b"EcoreX direct release DPAPI v1\0" + role.encode("ascii")


def _blob(payload: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    if not isinstance(payload, bytes) or not payload:
        raise RuntimeError("direct_release_key_material_invalid")
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    return _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _protect(payload: bytes, *, role: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("direct_release_dpapi_windows_required")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    source, source_buffer = _blob(payload)
    entropy, entropy_buffer = _blob(_entropy(role))
    output = _DataBlob()
    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "EcoreX v1 direct release key",
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok or not output.pbData or output.cbData < 1:
            raise RuntimeError("direct_release_dpapi_protect_failed")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if output.pbData:
            kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def _unprotect(payload: bytes, *, role: str) -> bytearray:
    if os.name != "nt":
        raise RuntimeError("direct_release_dpapi_windows_required")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    source, source_buffer = _blob(payload)
    entropy, entropy_buffer = _blob(_entropy(role))
    output = _DataBlob()
    description = wintypes.LPWSTR()
    try:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not ok or not output.pbData or output.cbData != 32:
            raise RuntimeError("direct_release_dpapi_unprotect_failed")
        return bytearray(ctypes.string_at(output.pbData, output.cbData))
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        if output.pbData:
            kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _read_regular(path: Path) -> bytes:
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= _MAX_KEY_DOCUMENT_BYTES
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise OSError
            payload = stream.read(_MAX_KEY_DOCUMENT_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise RuntimeError("direct_release_key_document_invalid") from None
    if (
        len(payload) != before.st_size
        or _identity(opened) != _identity(before)
        or _identity(after) != _identity(before)
        or _identity(current) != _identity(before)
    ):
        raise RuntimeError("direct_release_key_document_changed")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _key_path(root: Path, role: str) -> Path:
    if role not in _ROLES:
        raise RuntimeError("direct_release_key_role_invalid")
    return root / f"{role}-key.json"


def _real_key_root(value: Path, *, create: bool) -> Path:
    raw = value.absolute()
    try:
        if create:
            raw.mkdir(parents=True, exist_ok=True)
        metadata = raw.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise OSError
        resolved = raw.resolve(strict=True)
    except OSError:
        raise RuntimeError("direct_release_key_store_unavailable") from None
    if os.path.normcase(str(resolved)) != os.path.normcase(str(raw)):
        raise RuntimeError("direct_release_key_store_unavailable")
    return resolved


def _load_document(root: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_regular(_key_path(root, role)).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError("direct_release_key_document_invalid") from None
    expected = {
        "schema_version",
        "role",
        "algorithm",
        "key_id",
        "public_key_base64",
        "public_key_sha256",
        "protected_seed_base64",
        "protection",
        "created_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != _SCHEMA_VERSION
        or value.get("role") != role
        or value.get("algorithm") != "ed25519"
        or value.get("protection") != "windows-dpapi-current-user"
        or not isinstance(value.get("key_id"), str)
        or _KEY_ID.fullmatch(value["key_id"]) is None
        or not isinstance(value.get("created_at"), str)
    ):
        raise RuntimeError("direct_release_key_document_invalid")
    try:
        public = base64.b64decode(value["public_key_base64"], validate=True)
        protected = base64.b64decode(value["protected_seed_base64"], validate=True)
    except (TypeError, ValueError):
        raise RuntimeError("direct_release_key_document_invalid") from None
    if (
        len(public) != 32
        or len(protected) < 33
        or hashlib.sha256(public).hexdigest() != value.get("public_key_sha256")
    ):
        raise RuntimeError("direct_release_key_document_invalid")
    return value


def _atomic_create(path: Path, value: dict[str, Any]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if os.path.lexists(path):
        raise RuntimeError("direct_release_key_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _new_document(role: str) -> dict[str, Any]:
    key = Ed25519PrivateKey.generate()
    seed = bytearray(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    fingerprint = hashlib.sha256(public).hexdigest()
    try:
        protected = _protect(bytes(seed), role=role)
    finally:
        for index in range(len(seed)):
            seed[index] = 0
    return {
        "schema_version": _SCHEMA_VERSION,
        "role": role,
        "algorithm": "ed25519",
        "key_id": f"ecorex-direct-{role}-{fingerprint[:20]}",
        "public_key_base64": base64.b64encode(public).decode("ascii"),
        "public_key_sha256": fingerprint,
        "protected_seed_base64": base64.b64encode(protected).decode("ascii"),
        "protection": "windows-dpapi-current-user",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _initialize(root: Path | None = None) -> dict[str, Any]:
    key_root = _real_key_root(root or _key_root(), create=True)
    if any(os.path.lexists(_key_path(key_root, role)) for role in _ROLES):
        raise RuntimeError("direct_release_key_already_exists")
    os.chmod(key_root, 0o700)
    created: list[Path] = []
    try:
        for role in _ROLES:
            path = _key_path(key_root, role)
            _atomic_create(path, _new_document(role))
            created.append(path)
        result = _describe(key_root)
        if result["release"]["public_key_sha256"] == result["publication"]["public_key_sha256"]:
            raise RuntimeError("direct_release_keys_not_independent")
        return result
    except BaseException:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _describe(root: Path | None = None) -> dict[str, Any]:
    key_root = _real_key_root(root or _key_root(), create=False)
    result: dict[str, Any] = {"schema_version": _SCHEMA_VERSION}
    for role in _ROLES:
        document = _load_document(key_root, role)
        result[role] = {
            "algorithm": document["algorithm"],
            "key_id": document["key_id"],
            "public_key_base64": document["public_key_base64"],
            "public_key_sha256": document["public_key_sha256"],
            "protection": document["protection"],
            "created_at": document["created_at"],
        }
    if result["release"]["public_key_sha256"] == result["publication"]["public_key_sha256"]:
        raise RuntimeError("direct_release_keys_not_independent")
    return result


def _sign(payload: bytes, root: Path | None = None) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_SIGNING_PAYLOAD_BYTES:
        raise RuntimeError("direct_release_signing_payload_invalid")
    role = "publication" if payload.startswith(_PUBLICATION_DOMAIN) else "release"
    key_root = _real_key_root(root or _key_root(), create=False)
    document = _load_document(key_root, role)
    try:
        protected = base64.b64decode(document["protected_seed_base64"], validate=True)
    except (TypeError, ValueError):
        raise RuntimeError("direct_release_key_document_invalid") from None
    seed = _unprotect(protected, role=role)
    try:
        private = Ed25519PrivateKey.from_private_bytes(bytes(seed))
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if base64.b64encode(public).decode("ascii") != document["public_key_base64"]:
            raise RuntimeError("direct_release_key_document_invalid")
        signature = private.sign(payload)
    finally:
        for index in range(len(seed)):
            seed[index] = 0
    if len(signature) != 64:
        raise RuntimeError("direct_release_signature_invalid")
    return signature


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "initialize":
            value = _initialize()
            value["status"] = "initialized"
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "describe":
            value = _describe()
            value["status"] = "ready"
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
            return 0
        payload = sys.stdin.buffer.read(_MAX_SIGNING_PAYLOAD_BYTES + 1)
        signature = _sign(payload)
        sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\n")
        return 0
    except Exception:
        # DigestPinnedExternalSigner deliberately suppresses stderr details.
        # Keep this generic in case an operator invokes the adapter directly.
        sys.stderr.write("direct_release_signer_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

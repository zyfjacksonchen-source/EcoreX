#!/usr/bin/env python3
"""Create and use the local DPAPI-protected production administrator secret."""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.security.windows_dpapi import (  # noqa: E402
    protect_current_user,
    unprotect_current_user,
)


ENTROPY = b"EcoreX v1 production platform admin credential\0"
ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


def _path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise RuntimeError("admin_credential_store_unavailable")
    return Path(local) / "EcoreX" / "admin" / "production-v1.json"


def _initialize(account_id: str, path: Path | None = None) -> dict[str, Any]:
    if ACCOUNT.fullmatch(account_id) is None:
        raise RuntimeError("admin_credential_account_invalid")
    target = path or _path()
    if os.path.lexists(target):
        raise RuntimeError("admin_credential_already_exists")
    credential = bytearray(("adm_" + secrets.token_urlsafe(32)).encode("ascii"))
    try:
        protected = protect_current_user(
            bytes(credential),
            entropy=ENTROPY,
            description="EcoreX v1 production platform administrator",
        )
        value = {
            "schema_version": 1,
            "account_id": account_id,
            "credential_sha256": hashlib.sha256(credential).hexdigest(),
            "protected_credential_base64": base64.b64encode(protected).decode("ascii"),
            "protection": "windows-dpapi-current-user",
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        _atomic_create(target, value)
    finally:
        for index in range(len(credential)):
            credential[index] = 0
    return _description(value)


def _load(path: Path | None = None) -> tuple[dict[str, Any], bytearray]:
    target = path or _path()
    payload = _read_regular(target)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError("admin_credential_document_invalid") from None
    expected = {
        "schema_version",
        "account_id",
        "credential_sha256",
        "protected_credential_base64",
        "protection",
        "created_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or ACCOUNT.fullmatch(str(value.get("account_id"))) is None
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("credential_sha256")))
        or value.get("protection") != "windows-dpapi-current-user"
        or not isinstance(value.get("created_at"), str)
    ):
        raise RuntimeError("admin_credential_document_invalid")
    try:
        protected = base64.b64decode(
            value["protected_credential_base64"], validate=True
        )
        credential = unprotect_current_user(protected, entropy=ENTROPY)
    except (TypeError, ValueError):
        raise RuntimeError("admin_credential_document_invalid") from None
    if (
        not 32 <= len(credential) <= 128
        or hashlib.sha256(credential).hexdigest() != value["credential_sha256"]
    ):
        for index in range(len(credential)):
            credential[index] = 0
        raise RuntimeError("admin_credential_document_invalid")
    return value, credential


def _copy(path: Path | None = None) -> dict[str, Any]:
    value, credential = _load(path)
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(credential.decode("ascii"))
        finally:
            win32clipboard.CloseClipboard()
    finally:
        for index in range(len(credential)):
            credential[index] = 0
    return {**_description(value), "copied_to_clipboard": True}


def _description(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "account_id": value["account_id"],
        "credential_sha256": value["credential_sha256"],
        "protection": value["protection"],
        "created_at": value["created_at"],
    }


def _read_regular(path: Path) -> bytes:
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= 64 * 1024
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(64 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise RuntimeError("admin_credential_document_invalid") from None
    if not (
        len(payload) == before.st_size
        and _identity(before) == _identity(opened) == _identity(after) == _identity(current)
    ):
        raise RuntimeError("admin_credential_document_changed")
    return payload


def _atomic_create(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".admin.", suffix=".tmp")
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish by a same-directory hard link so two concurrent operator
        # initializations cannot replace each other's credential after a
        # check-then-write race.  The temporary inode is already fsynced.
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise RuntimeError("admin_credential_already_exists") from None
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("initialize", "describe", "copy"))
    parser.add_argument("--account-id", default="ecorex-platform-admin")
    args = parser.parse_args()
    try:
        if args.command == "initialize":
            result = _initialize(args.account_id)
        elif args.command == "copy":
            result = _copy()
        else:
            value, credential = _load()
            try:
                result = _description(value)
            finally:
                for index in range(len(credential)):
                    credential[index] = 0
        print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print('{"ok":false,"code":"admin_credential_operation_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

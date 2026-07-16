"""Minimal stdin-only Ed25519 signer for one attested encrypted server volume.

This is the explicit direct-operator fallback when a managed KMS/HSM is not
available.  It does not claim HSM protection: private seeds are ordinary files
whose use is fenced to an encrypted-volume attestation digest, strict file
ownership and one hard-coded adapter role.
"""

from __future__ import annotations

import base64
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


SCHEMA_VERSION = 1
ROLES = ("publication", "rollback", "device-access", "device-lease")
MAX_DOCUMENT_BYTES = 16 * 1024
MAX_SIGNING_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EncryptedVolumeSignerError(RuntimeError):
    pass


def initialize_keyring(root: Path, *, attestation_sha256: str) -> dict[str, Any]:
    key_root = _key_root(root, create=True)
    if _SHA256.fullmatch(attestation_sha256) is None:
        raise EncryptedVolumeSignerError("server_signer_attestation_invalid")
    if any(os.path.lexists(_key_path(key_root, role)) for role in ROLES):
        raise EncryptedVolumeSignerError("server_signer_keyring_exists")
    _set_private_mode(key_root, 0o700)
    created: list[Path] = []
    try:
        for role in ROLES:
            private = Ed25519PrivateKey.generate()
            seed = bytearray(
                private.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            )
            public = private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            digest = hashlib.sha256(public).hexdigest()
            try:
                document = {
                    "schema_version": SCHEMA_VERSION,
                    "role": role,
                    "algorithm": "ed25519",
                    "key_id": f"ecorex-server-{role}-{digest[:20]}",
                    "public_key_base64": base64.b64encode(public).decode("ascii"),
                    "public_key_sha256": digest,
                    "private_seed_base64": base64.b64encode(seed).decode("ascii"),
                    "protection": "attested-encrypted-volume-software-key",
                    "encryption_attestation_sha256": attestation_sha256,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
                path = _key_path(key_root, role)
                _atomic_create(path, document)
                created.append(path)
            finally:
                for index in range(len(seed)):
                    seed[index] = 0
        return describe_keyring(key_root)
    except BaseException:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def describe_keyring(root: Path) -> dict[str, Any]:
    key_root = _key_root(root, create=False)
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": "ready"}
    fingerprints: set[str] = set()
    for role in ROLES:
        value = _load_document(key_root, role)
        fingerprint = str(value["public_key_sha256"])
        if fingerprint in fingerprints:
            raise EncryptedVolumeSignerError("server_signer_keys_not_independent")
        fingerprints.add(fingerprint)
        result[role] = {
            "schema_version": SCHEMA_VERSION,
            "role": role,
            "algorithm": "ed25519",
            "key_id": value["key_id"],
            "public_key_base64": value["public_key_base64"],
            "public_key_sha256": fingerprint,
            "encryption_attestation_sha256": value[
                "encryption_attestation_sha256"
            ],
            "protection": value["protection"],
            "created_at": value["created_at"],
        }
    return result


def public_key_description(root: Path, *, role: str) -> dict[str, Any]:
    """Return the bounded public-only cross-boundary release description."""

    if role not in ROLES:
        raise EncryptedVolumeSignerError("server_signer_role_invalid")
    value = _load_document(_key_root(root, create=False), role)
    return {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "algorithm": "ed25519",
        "key_id": value["key_id"],
        "public_key_base64": value["public_key_base64"],
        "public_key_sha256": value["public_key_sha256"],
    }


def sign_for_role(
    role: str,
    payload: bytes,
    *,
    root: Path,
    expected_attestation_sha256: str,
) -> bytes:
    if role not in ROLES:
        raise EncryptedVolumeSignerError("server_signer_role_invalid")
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_SIGNING_BYTES:
        raise EncryptedVolumeSignerError("server_signer_payload_invalid")
    if _SHA256.fullmatch(expected_attestation_sha256) is None:
        raise EncryptedVolumeSignerError("server_signer_attestation_invalid")
    value = _load_document(_key_root(root, create=False), role)
    if value["encryption_attestation_sha256"] != expected_attestation_sha256:
        raise EncryptedVolumeSignerError("server_signer_attestation_mismatch")
    try:
        seed = bytearray(
            base64.b64decode(value["private_seed_base64"], validate=True)
        )
    except (TypeError, ValueError):
        raise EncryptedVolumeSignerError("server_signer_document_invalid") from None
    if len(seed) != 32:
        raise EncryptedVolumeSignerError("server_signer_document_invalid")
    try:
        private = Ed25519PrivateKey.from_private_bytes(bytes(seed))
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if hashlib.sha256(public).hexdigest() != value["public_key_sha256"]:
            raise EncryptedVolumeSignerError("server_signer_document_invalid")
        signature = private.sign(payload)
    finally:
        for index in range(len(seed)):
            seed[index] = 0
    if len(signature) != 64:
        raise EncryptedVolumeSignerError("server_signer_signature_invalid")
    return signature


def adapter_main(role: str, argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        root_value = os.environ.get("ECOREX_SERVER_SIGNER_KEY_ROOT")
        attestation = os.environ.get(
            "ECOREX_SERVER_SIGNER_ENCRYPTION_ATTESTATION_SHA256"
        )
        if not root_value or not attestation or arguments:
            raise EncryptedVolumeSignerError("server_signer_configuration_invalid")
        payload = sys.stdin.buffer.read(MAX_SIGNING_BYTES + 1)
        signature = sign_for_role(
            role,
            payload,
            root=Path(root_value),
            expected_attestation_sha256=attestation,
        )
        sys.stdout.write(base64.b64encode(signature).decode("ascii") + "\n")
        return 0
    except Exception:
        sys.stderr.write("server_signer_failed\n")
        return 1


def _key_path(root: Path, role: str) -> Path:
    if role not in ROLES:
        raise EncryptedVolumeSignerError("server_signer_role_invalid")
    return root / f"{role}.json"


def _key_root(value: Path, *, create: bool) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise EncryptedVolumeSignerError("server_signer_key_root_invalid")
    raw = value.absolute()
    try:
        if create:
            raw.mkdir(parents=True, exist_ok=True)
        metadata = raw.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        if metadata.st_mode & 0o077:
            raise OSError
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise OSError
        resolved = raw.resolve(strict=True)
    except OSError:
        raise EncryptedVolumeSignerError("server_signer_key_root_invalid") from None
    if os.path.normcase(str(resolved)) != os.path.normcase(str(raw)):
        raise EncryptedVolumeSignerError("server_signer_key_root_invalid")
    return resolved


def _load_document(root: Path, role: str) -> dict[str, Any]:
    path = _key_path(root, role)
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= MAX_DOCUMENT_BYTES
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
        ):
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(MAX_DOCUMENT_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise EncryptedVolumeSignerError("server_signer_document_invalid") from None
    if (
        len(payload) != before.st_size
        or _file_identity(before) != _file_identity(opened)
        or _file_identity(before) != _file_identity(after)
        or _file_identity(before) != _file_identity(current)
    ):
        raise EncryptedVolumeSignerError("server_signer_document_changed")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise EncryptedVolumeSignerError("server_signer_document_invalid") from None
    expected = {
        "schema_version",
        "role",
        "algorithm",
        "key_id",
        "public_key_base64",
        "public_key_sha256",
        "private_seed_base64",
        "protection",
        "encryption_attestation_sha256",
        "created_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("role") != role
        or value.get("algorithm") != "ed25519"
        or value.get("protection") != "attested-encrypted-volume-software-key"
        or not isinstance(value.get("key_id"), str)
        or _KEY_ID.fullmatch(value["key_id"]) is None
        or not isinstance(value.get("public_key_sha256"), str)
        or _SHA256.fullmatch(value["public_key_sha256"]) is None
        or not isinstance(value.get("encryption_attestation_sha256"), str)
        or _SHA256.fullmatch(value["encryption_attestation_sha256"]) is None
        or not isinstance(value.get("created_at"), str)
    ):
        raise EncryptedVolumeSignerError("server_signer_document_invalid")
    try:
        public = base64.b64decode(value["public_key_base64"], validate=True)
        seed = base64.b64decode(value["private_seed_base64"], validate=True)
    except (TypeError, ValueError):
        raise EncryptedVolumeSignerError("server_signer_document_invalid") from None
    if (
        len(public) != 32
        or len(seed) != 32
        or hashlib.sha256(public).hexdigest() != value["public_key_sha256"]
    ):
        raise EncryptedVolumeSignerError("server_signer_document_invalid")
    return value


def _atomic_create(path: Path, value: dict[str, Any]) -> None:
    if os.path.lexists(path):
        raise EncryptedVolumeSignerError("server_signer_keyring_exists")
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _set_private_mode(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _set_private_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)
    if path.stat().st_mode & 0o077:
        raise EncryptedVolumeSignerError("server_signer_permissions_invalid")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


__all__ = [
    "EncryptedVolumeSignerError",
    "ROLES",
    "adapter_main",
    "describe_keyring",
    "initialize_keyring",
    "public_key_description",
    "sign_for_role",
]

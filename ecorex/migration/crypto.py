"""Fail-closed local encryption for legacy credentials.

The encryption key is supplied by the caller (normally a platform credential
vault) and is never stored in the migration target.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True)
class SecretRecord:
    source_relative_path: str
    key_path: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_relative_path": self.source_relative_path,
            "key_path": self.key_path,
            "value": self.value,
        }


_EXACT_SECRET_NAMES = frozenset(
    {
        "apikey",
        "api_key",
        "token",
        "secret",
        "password",
        "authorization",
        "cookie",
        "private_key",
        "access_key",
        "refresh_token",
        "client_secret",
        "app_secret",
        "bot_token",
        "aes_key",
    }
)


def _normalized_key(value: str) -> str:
    output: list[str] = []
    for character in str(value or ""):
        if character.isupper() and output and output[-1] != "_":
            output.append("_")
        output.append(character.casefold() if character.isalnum() else "_")
    return "_".join(part for part in "".join(output).split("_") if part)


def is_secret_key(key: str) -> bool:
    normalized = _normalized_key(key)
    compact = normalized.replace("_", "")
    if normalized in _EXACT_SECRET_NAMES or compact in _EXACT_SECRET_NAMES:
        return True
    return (
        normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or normalized.endswith("_password")
        or normalized.endswith("_authorization")
        or normalized.endswith("_private_key")
        or normalized.endswith("_access_key")
        or normalized.endswith("_aes_key")
    )


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def collect_secrets(
    value: Any,
    *,
    source_relative_path: str,
    prefix: tuple[str, ...] = (),
) -> tuple[SecretRecord, ...]:
    records: list[SecretRecord] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = (*prefix, key)
            if is_secret_key(key):
                if _has_value(child):
                    records.append(
                        SecretRecord(
                            source_relative_path=source_relative_path,
                            key_path=".".join(path),
                            value=child,
                        )
                    )
                continue
            records.extend(
                collect_secrets(
                    child,
                    source_relative_path=source_relative_path,
                    prefix=path,
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            records.extend(
                collect_secrets(
                    child,
                    source_relative_path=source_relative_path,
                    prefix=(*prefix, str(index)),
                )
            )
    return tuple(records)


def _validate_key(key: bytes) -> bytes:
    material = bytes(key)
    if len(material) not in {16, 24, 32}:
        raise ValueError("AES-GCM quarantine key must contain 16, 24, or 32 bytes")
    return material


def encrypt_quarantine(
    records: Iterable[SecretRecord],
    *,
    key: bytes,
    associated_digest: str,
    destination: Path,
) -> None:
    material = _validate_key(key)
    serialized = json.dumps(
        {
            "schema_version": 1,
            "source_inventory_digest": associated_digest,
            "entries": [item.to_dict() for item in records],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(material).encrypt(
        nonce,
        serialized,
        associated_digest.encode("ascii"),
    )
    envelope = {
        "schema_version": 1,
        "algorithm": "AES-256-GCM" if len(material) == 32 else "AES-GCM",
        "associated_digest": associated_digest,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def decrypt_quarantine(path: str | Path, *, key: bytes) -> dict[str, Any]:
    """Test/admin recovery primitive; the normal migration path never decrypts."""

    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    associated_digest = str(envelope["associated_digest"])
    plaintext = AESGCM(_validate_key(key)).decrypt(
        base64.urlsafe_b64decode(envelope["nonce"]),
        base64.urlsafe_b64decode(envelope["ciphertext"]),
        associated_digest.encode("ascii"),
    )
    return json.loads(plaintext.decode("utf-8"))


def load_quarantine_key(path: str | Path) -> bytes:
    raw = Path(path).read_bytes().strip()
    if len(raw) in {16, 24, 32}:
        return raw
    text = raw.decode("ascii").strip()
    try:
        decoded = bytes.fromhex(text)
        if len(decoded) in {16, 24, 32}:
            return decoded
    except ValueError:
        pass
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        if len(decoded) in {16, 24, 32}:
            return decoded
    except (ValueError, TypeError):
        pass
    raise ValueError("quarantine key file must contain raw, hex, or base64 AES key material")

"""In-memory and injectable signing boundary for release publication."""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from ecorex.update import SignatureEnvelope


_PLACEHOLDER_SIGNATURE = base64.b64encode(b"\0" * 64).decode("ascii")


class SigningError(RuntimeError):
    pass


@runtime_checkable
class ReleaseSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> bytes:
        """Return a raw 64-byte Ed25519 signature for ``payload``."""

        ...


class Ed25519MemorySigner:
    """Signer backed only by an injected in-memory Ed25519 key object."""

    __slots__ = ("_key_id", "__key")

    def __init__(self, key_id: str, key: Ed25519PrivateKey) -> None:
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("key must be an in-memory Ed25519PrivateKey object")
        # Reuse the release envelope's strict key-id validation without ever
        # serializing the secret key.
        SignatureEnvelope("ed25519", key_id, _PLACEHOLDER_SIGNATURE)
        self._key_id = key_id
        self.__key = key

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def public_key_bytes(self) -> bytes:
        return self.__key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise SigningError("release signing payload must be non-empty bytes")
        signature = self.__key.sign(payload)
        if len(signature) != 64:
            raise SigningError("Ed25519 signer returned a non-64-byte signature")
        return signature

    def __repr__(self) -> str:
        return f"<Ed25519MemorySigner key_id={self._key_id!r} secret=<redacted>>"


def sign_envelope(signer: ReleaseSigner, payload: bytes) -> SignatureEnvelope:
    try:
        key_id = signer.key_id
    except Exception as exc:
        # Signers may be backed by an HSM/KMS adapter. Never retain its raw
        # exception as a cause because callers commonly log exception chains.
        raise SigningError(
            f"release signer failed safely: {type(exc).__name__}"
        ) from None
    try:
        SignatureEnvelope("ed25519", key_id, _PLACEHOLDER_SIGNATURE)
    except (TypeError, ValueError) as exc:
        raise SigningError(
            f"release signer identity is invalid: {type(exc).__name__}"
        ) from None
    try:
        signature = signer.sign(payload)
    except Exception as exc:
        raise SigningError(
            f"release signer failed safely: {type(exc).__name__}"
        ) from None
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise SigningError("release signer must return exactly 64 signature bytes")
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id=key_id,
        value=base64.b64encode(signature).decode("ascii"),
    )

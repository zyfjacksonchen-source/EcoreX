"""Cryptographic and content verification for signed EcoreX releases."""

from __future__ import annotations

import base64
import hashlib
import os
import stat as stat_module
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from .manifest import ReleaseArtifact, ReleaseManifest, SignatureEnvelope


class VerificationError(RuntimeError):
    """Base class for a release that cannot be trusted."""


class SignatureVerificationError(VerificationError):
    pass


class ContentVerificationError(VerificationError):
    pass


class VerifierUnavailable(SignatureVerificationError):
    pass


@runtime_checkable
class SignatureVerifier(Protocol):
    """Trust-provider interface.

    Implementations must raise :class:`SignatureVerificationError` for every
    invalid signature and return the literal boolean ``True`` on success.
    Ambiguous ``None``/truthy verdicts are rejected at this trust boundary.
    """

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool: ...


class RejectingSignatureVerifier:
    """Safe default used when no production trust provider was configured."""

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        del payload, signature
        raise VerifierUnavailable(
            "no signature verifier is configured; unsigned or unverified updates are forbidden"
        )


class Ed25519SignatureVerifier:
    """Real Ed25519 verification backed by the optional ``cryptography`` package.

    The dependency is imported eagerly in ``__init__``.  If it is unavailable,
    construction fails closed instead of silently accepting a release.
    Public keys are raw 32-byte Ed25519 keys indexed by manifest ``key_id``.
    """

    def __init__(self, public_keys: Mapping[str, bytes]) -> None:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError as exc:  # pragma: no cover - depends on deployment pack
            raise VerifierUnavailable(
                "Ed25519 verification requires the signed crypto capability pack"
            ) from exc

        if not public_keys:
            raise VerifierUnavailable(
                "at least one trusted Ed25519 public key is required"
            )
        parsed: dict[str, object] = {}
        fingerprints: dict[str, str] = {}
        for key_id, raw_key in public_keys.items():
            if not isinstance(raw_key, bytes) or len(raw_key) != 32:
                raise ValueError(
                    f"Ed25519 public key {key_id!r} must contain exactly 32 bytes"
                )
            parsed[key_id] = Ed25519PublicKey.from_public_bytes(raw_key)
            fingerprints[key_id] = hashlib.sha256(raw_key).hexdigest()
        self._keys = parsed
        self._fingerprints = fingerprints
        self._invalid_signature_type = InvalidSignature

    def key_fingerprint(self, key_id: str) -> str | None:
        """Return a non-secret raw-key fingerprint for trust-role separation."""

        return self._fingerprints.get(key_id)

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        if signature.algorithm != "ed25519":
            raise SignatureVerificationError(
                f"unsupported signature algorithm: {signature.algorithm!r}"
            )
        public_key = self._keys.get(signature.key_id)
        if public_key is None:
            raise SignatureVerificationError(
                f"signature key is not trusted: {signature.key_id!r}"
            )
        try:
            detached = base64.b64decode(signature.value, validate=True)
            # Runtime object is an Ed25519PublicKey; kept structural to avoid a
            # hard import dependency at module import time.
            public_key.verify(detached, payload)  # type: ignore[attr-defined]
        except (ValueError, self._invalid_signature_type) as exc:
            raise SignatureVerificationError(
                "detached Ed25519 signature is invalid"
            ) from exc
        return True


def verify_manifest_signature(
    manifest: ReleaseManifest,
    verifier: SignatureVerifier,
) -> None:
    _require_positive_verdict(
        verifier.verify(manifest.canonical_payload(), manifest.signature),
        "release manifest signature was rejected",
    )


def verify_artifact_signature(
    manifest: ReleaseManifest,
    artifact: ReleaseArtifact,
    verifier: SignatureVerifier,
) -> None:
    payload = artifact.signed_payload(
        release_id=manifest.release_id,
        version=manifest.version,
        build_digest=manifest.build_digest,
    )
    _require_positive_verdict(
        verifier.verify(payload, artifact.signature),
        f"artifact signature was rejected for {artifact.artifact_id!r}",
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    before = path.lstat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ContentVerificationError("artifact changed while it was being opened")
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise ContentVerificationError("artifact changed while it was being verified")
    return digest.hexdigest()


def verify_artifact_file(
    path: Path,
    manifest: ReleaseManifest,
    artifact: ReleaseArtifact,
    verifier: SignatureVerifier,
) -> None:
    """Verify size, SHA-256, and detached signature before staging."""

    try:
        stat = path.lstat()
    except OSError as exc:
        raise ContentVerificationError(f"artifact cannot be read: {path}") from exc
    attributes = getattr(stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(stat.st_mode)
        or stat_module.S_ISLNK(stat.st_mode)
        or bool(attributes & reparse_flag)
    ):
        raise ContentVerificationError(f"artifact is not a regular file: {path}")
    if stat.st_size != artifact.size_bytes:
        raise ContentVerificationError(
            f"artifact size mismatch: expected {artifact.size_bytes}, got {stat.st_size}"
        )
    actual = sha256_file(path)
    if actual != artifact.sha256:
        raise ContentVerificationError(
            f"artifact SHA-256 mismatch: expected {artifact.sha256}, got {actual}"
        )
    verify_artifact_signature(manifest, artifact, verifier)


def _require_positive_verdict(verdict: object, message: str) -> None:
    if verdict is not True:
        raise SignatureVerificationError(
            f"{message}; verifier must return explicit True"
        )

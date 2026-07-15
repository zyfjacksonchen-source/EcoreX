"""Signed, single-use administrator rollback authorizations.

The release manifest proves *what* bytes a client may install.  A rollback
authorization proves *why* an otherwise non-monotonic target may be admitted.
The two signatures deliberately use separate trust rings.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import threading
from typing import Any, Protocol
import uuid

from .manifest import ReleaseManifest, SignatureEnvelope
from .verification import SignatureVerifier


ROLLBACK_AUTHORIZATION_SCHEMA_VERSION = 1
ROLLBACK_AUTHORIZATION_DEFAULT_TTL_SECONDS = 300
ROLLBACK_AUTHORIZATION_MAX_TTL_SECONDS = 900
ROLLBACK_AUTHORIZATION_HEADER = "X-EcoreX-Rollback-Authorization"
_TOKEN_PREFIX = "erb1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_TOKEN_BYTES = 8192
_CLAIM_KEYS = frozenset(
    {
        "schema_version",
        "authorization_id",
        "rollback_id",
        "client_id",
        "source_release_id",
        "source_version",
        "source_build_digest",
        "source_artifact_id",
        "source_artifact_sha256",
        "target_release_id",
        "target_version",
        "target_build_digest",
        "target_artifact_id",
        "target_artifact_sha256",
        "channel",
        "platform",
        "architecture",
        "request_nonce",
        "issued_at",
        "expires_at",
    }
)


class RollbackAuthorizationError(RuntimeError):
    """The rollback grant is malformed, expired, replayed, or unauthorized."""


class RollbackAuthorizationSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RollbackAuthorizationClaims:
    schema_version: int
    authorization_id: str
    rollback_id: str
    client_id: str
    source_release_id: str
    source_version: str
    source_build_digest: str
    source_artifact_id: str
    source_artifact_sha256: str
    target_release_id: str
    target_version: str
    target_build_digest: str
    target_artifact_id: str
    target_artifact_sha256: str
    channel: str
    platform: str
    architecture: str
    request_nonce: str
    issued_at: int
    expires_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in sorted(_CLAIM_KEYS)
        }


def issue_rollback_authorization(
    *,
    signer: RollbackAuthorizationSigner,
    rollback_id: str,
    client_id: str,
    source_manifest: ReleaseManifest,
    target_manifest: ReleaseManifest,
    platform: str,
    architecture: str,
    request_nonce: str,
    ttl_seconds: int = ROLLBACK_AUTHORIZATION_DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """Issue a compact, header-safe grant bound to one authenticated request."""

    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 60 <= ttl_seconds <= ROLLBACK_AUTHORIZATION_MAX_TTL_SECONDS
    ):
        raise RollbackAuthorizationError("rollback authorization TTL is invalid")
    for label, value in (
        ("rollback_id", rollback_id),
        ("client_id", client_id),
        ("platform", platform),
        ("architecture", architecture),
    ):
        if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
            raise RollbackAuthorizationError(f"rollback {label} is invalid")
    if not isinstance(request_nonce, str) or _SAFE_NONCE.fullmatch(request_nonce) is None:
        raise RollbackAuthorizationError("rollback request nonce is invalid")
    if source_manifest.channel is not target_manifest.channel:
        raise RollbackAuthorizationError("rollback release channels differ")
    try:
        source_artifact = source_manifest.artifact(f"core-{platform}-{architecture}")
        target_artifact = target_manifest.artifact(f"core-{platform}-{architecture}")
    except Exception:
        raise RollbackAuthorizationError("rollback target matrix is incompatible") from None
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise RollbackAuthorizationError("rollback issue time must be timezone-aware")
    issued_at = int(instant.timestamp())
    claims = RollbackAuthorizationClaims(
        schema_version=ROLLBACK_AUTHORIZATION_SCHEMA_VERSION,
        authorization_id="rba_" + uuid.uuid4().hex,
        rollback_id=rollback_id,
        client_id=client_id,
        source_release_id=source_manifest.release_id,
        source_version=source_manifest.version,
        source_build_digest=source_manifest.build_digest,
        source_artifact_id=source_artifact.artifact_id,
        source_artifact_sha256=source_artifact.sha256,
        target_release_id=target_manifest.release_id,
        target_version=target_manifest.version,
        target_build_digest=target_manifest.build_digest,
        target_artifact_id=target_artifact.artifact_id,
        target_artifact_sha256=target_artifact.sha256,
        channel=target_manifest.channel.value,
        platform=platform,
        architecture=architecture,
        request_nonce=request_nonce,
        issued_at=issued_at,
        expires_at=issued_at + ttl_seconds,
    )
    payload = _signing_payload(claims.to_dict())
    try:
        key_id = signer.key_id
        signature = signer.sign(payload)
    except Exception as error:
        raise RollbackAuthorizationError(
            f"rollback signer failed safely: {type(error).__name__}"
        ) from None
    if (
        not isinstance(key_id, str)
        or _SAFE_ID.fullmatch(key_id) is None
        or not isinstance(signature, bytes)
        or len(signature) != 64
    ):
        raise RollbackAuthorizationError("rollback signer response is invalid")
    token = ".".join(
        (_TOKEN_PREFIX, _b64url(_canonical(claims.to_dict())), key_id, _b64url(signature))
    )
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
        raise RollbackAuthorizationError("rollback authorization is too large")
    return token


class RollbackAuthorizationVerifier:
    """Stateless cryptographic verifier used at transport and install boundaries."""

    def __init__(
        self,
        verifier: SignatureVerifier,
        *,
        clock: Callable[[], datetime] | None = None,
        clock_skew_seconds: int = 30,
    ) -> None:
        if not 0 <= clock_skew_seconds <= 120:
            raise ValueError("rollback authorization clock skew is invalid")
        self.verifier = verifier
        self.clock = clock or (lambda: datetime.now(UTC))
        self.clock_skew_seconds = clock_skew_seconds

    def verify(
        self,
        token: str,
        *,
        current: Mapping[str, Any],
        target: ReleaseManifest,
        platform: str,
        architecture: str,
        expected_nonce: str | None = None,
        expected_client_id: str | None = None,
    ) -> RollbackAuthorizationClaims:
        claims, signature = _decode(token)
        try:
            verdict = self.verifier.verify(
                _signing_payload(claims.to_dict()), signature
            )
        except Exception:
            raise RollbackAuthorizationError(
                "rollback authorization signature is invalid"
            ) from None
        if verdict is not True:
            raise RollbackAuthorizationError(
                "rollback authorization verifier rejected the signature"
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RollbackAuthorizationError("rollback verifier clock is invalid")
        timestamp = int(now.timestamp())
        if claims.issued_at > timestamp + self.clock_skew_seconds:
            raise RollbackAuthorizationError("rollback authorization is not active")
        if claims.expires_at < timestamp - self.clock_skew_seconds:
            raise RollbackAuthorizationError("rollback authorization has expired")
        if not 60 <= claims.expires_at - claims.issued_at <= ROLLBACK_AUTHORIZATION_MAX_TTL_SECONDS:
            raise RollbackAuthorizationError("rollback authorization lifetime is invalid")
        try:
            target_artifact = target.artifact(f"core-{platform}-{architecture}")
        except Exception:
            raise RollbackAuthorizationError(
                "rollback authorization target artifact is absent"
            ) from None
        expected_current = {
            "release_id": claims.source_release_id,
            "version": claims.source_version,
            "build_digest": claims.source_build_digest,
            "artifact_id": claims.source_artifact_id,
            "artifact_sha256": claims.source_artifact_sha256,
            "channel": claims.channel,
        }
        if any(current.get(key) != value for key, value in expected_current.items()):
            raise RollbackAuthorizationError(
                "rollback authorization source identity differs"
            )
        if (
            claims.target_release_id != target.release_id
            or claims.target_version != target.version
            or claims.target_build_digest != target.build_digest
            or claims.target_artifact_id != target_artifact.artifact_id
            or claims.target_artifact_sha256 != target_artifact.sha256
            or claims.channel != target.channel.value
            or claims.platform != platform
            or claims.architecture != architecture
        ):
            raise RollbackAuthorizationError(
                "rollback authorization target identity differs"
            )
        if expected_nonce is not None and claims.request_nonce != expected_nonce:
            raise RollbackAuthorizationError("rollback authorization nonce differs")
        if expected_client_id is not None and claims.client_id != expected_client_id:
            raise RollbackAuthorizationError("rollback authorization client differs")
        return claims


class SingleUseRollbackAuthorizer:
    """Bridges a nonce-verified feed response to exactly one install admission."""

    def __init__(
        self,
        verifier: RollbackAuthorizationVerifier,
        *,
        platform: str,
        architecture: str,
        maximum_pending: int = 32,
    ) -> None:
        if not 1 <= maximum_pending <= 1024:
            raise ValueError("rollback authorization pending bound is invalid")
        self.verifier = verifier
        self.platform = platform
        self.architecture = architecture
        self.maximum_pending = maximum_pending
        self._lock = threading.RLock()
        self._accepted: dict[str, RollbackAuthorizationClaims] = {}

    def accept(
        self,
        token: str,
        *,
        current: Mapping[str, Any],
        target: ReleaseManifest,
        expected_nonce: str,
    ) -> RollbackAuthorizationClaims:
        claims = self.verifier.verify(
            token,
            current=current,
            target=target,
            platform=self.platform,
            architecture=self.architecture,
            expected_nonce=expected_nonce,
        )
        fingerprint = _token_fingerprint(token)
        with self._lock:
            self._remove_expired_locked()
            if len(self._accepted) >= self.maximum_pending and fingerprint not in self._accepted:
                oldest = min(
                    self._accepted,
                    key=lambda item: self._accepted[item].expires_at,
                )
                del self._accepted[oldest]
            self._accepted[fingerprint] = claims
        return claims

    def authorize(
        self,
        current: Mapping[str, Any],
        target: ReleaseManifest,
        token: str,
    ) -> bool:
        fingerprint = _token_fingerprint(token)
        with self._lock:
            self._remove_expired_locked()
            accepted = self._accepted.pop(fingerprint, None)
        if accepted is None:
            return False
        try:
            verified = self.verifier.verify(
                token,
                current=current,
                target=target,
                platform=self.platform,
                architecture=self.architecture,
            )
        except RollbackAuthorizationError:
            return False
        return verified == accepted

    def _remove_expired_locked(self) -> None:
        now = int(self.verifier.clock().timestamp()) - self.verifier.clock_skew_seconds
        for fingerprint, claims in tuple(self._accepted.items()):
            if claims.expires_at < now:
                del self._accepted[fingerprint]


def _decode(token: str) -> tuple[RollbackAuthorizationClaims, SignatureEnvelope]:
    if (
        not isinstance(token, str)
        or not 1 <= len(token.encode("utf-8")) <= _MAX_TOKEN_BYTES
    ):
        raise RollbackAuthorizationError("rollback authorization size is invalid")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_PREFIX:
        raise RollbackAuthorizationError("rollback authorization format is invalid")
    encoded_payload, key_id, encoded_signature = parts[1:]
    if _SAFE_ID.fullmatch(key_id) is None:
        raise RollbackAuthorizationError("rollback authorization key is invalid")
    payload = _unb64url(encoded_payload)
    raw_signature = _unb64url(encoded_signature)
    if len(payload) > 4096 or len(raw_signature) != 64:
        raise RollbackAuthorizationError("rollback authorization payload is invalid")
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RollbackAuthorizationError("rollback authorization JSON is invalid") from None
    if not isinstance(raw, dict) or set(raw) != _CLAIM_KEYS or _canonical(raw) != payload:
        raise RollbackAuthorizationError("rollback authorization claims are invalid")
    claims = _claims(raw)
    return claims, SignatureEnvelope(
        algorithm="ed25519",
        key_id=key_id,
        value=base64.b64encode(raw_signature).decode("ascii"),
    )


def _claims(raw: Mapping[str, Any]) -> RollbackAuthorizationClaims:
    if raw.get("schema_version") != ROLLBACK_AUTHORIZATION_SCHEMA_VERSION:
        raise RollbackAuthorizationError("rollback authorization schema is unsupported")
    string_fields = _CLAIM_KEYS - {"schema_version", "issued_at", "expires_at"}
    if any(not isinstance(raw.get(field), str) for field in string_fields):
        raise RollbackAuthorizationError("rollback authorization strings are invalid")
    for field in (
        "authorization_id",
        "rollback_id",
        "client_id",
        "source_release_id",
        "source_artifact_id",
        "target_release_id",
        "target_artifact_id",
        "platform",
        "architecture",
    ):
        if _SAFE_ID.fullmatch(str(raw[field])) is None:
            raise RollbackAuthorizationError("rollback authorization identity is invalid")
    for field in (
        "source_build_digest",
        "source_artifact_sha256",
        "target_build_digest",
        "target_artifact_sha256",
    ):
        if _SHA256.fullmatch(str(raw[field])) is None:
            raise RollbackAuthorizationError("rollback authorization digest is invalid")
    if _SAFE_NONCE.fullmatch(str(raw["request_nonce"])) is None:
        raise RollbackAuthorizationError("rollback authorization nonce is invalid")
    if raw["channel"] not in {"canary", "stable"}:
        raise RollbackAuthorizationError("rollback authorization channel is invalid")
    for field in ("issued_at", "expires_at"):
        if isinstance(raw.get(field), bool) or not isinstance(raw.get(field), int):
            raise RollbackAuthorizationError("rollback authorization time is invalid")
    try:
        return RollbackAuthorizationClaims(**raw)
    except TypeError:
        raise RollbackAuthorizationError("rollback authorization claims are invalid") from None


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _signing_payload(value: Mapping[str, Any]) -> bytes:
    return b"ecorex-rollback-authorization-v1\n" + _canonical(value) + b"\n"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    if not value or _B64URL.fullmatch(value) is None:
        raise RollbackAuthorizationError("rollback authorization encoding is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError):
        raise RollbackAuthorizationError("rollback authorization encoding is invalid") from None
    if _b64url(decoded) != value:
        raise RollbackAuthorizationError("rollback authorization encoding is not canonical")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate rollback authorization field")
        result[key] = value
    return result


def _token_fingerprint(token: str) -> str:
    if not isinstance(token, str):
        return ""
    return hashlib.sha256(token.encode("utf-8", errors="strict")).hexdigest()


__all__ = [
    "ROLLBACK_AUTHORIZATION_DEFAULT_TTL_SECONDS",
    "ROLLBACK_AUTHORIZATION_HEADER",
    "ROLLBACK_AUTHORIZATION_MAX_TTL_SECONDS",
    "RollbackAuthorizationClaims",
    "RollbackAuthorizationError",
    "RollbackAuthorizationSigner",
    "RollbackAuthorizationVerifier",
    "SingleUseRollbackAuthorizer",
    "issue_rollback_authorization",
]

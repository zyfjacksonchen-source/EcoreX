"""Fail-closed verification for cloud-issued managed-session leases."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hmac
from typing import Mapping, Protocol, runtime_checkable

from .models import (
    LeaseSignatureError,
    LeaseValidationError,
    SignedManagedSessionLease,
    token_digest,
)


@runtime_checkable
class SessionLeaseVerifier(Protocol):
    def verify_identity(
        self,
        lease: SignedManagedSessionLease,
        *,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        ...

    def verify(
        self,
        lease: SignedManagedSessionLease,
        *,
        now: datetime,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        ...


class RejectingSessionLeaseVerifier:
    def verify_identity(
        self,
        lease: SignedManagedSessionLease,
        *,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        del lease, access_token, refresh_token, expected_digest
        raise LeaseSignatureError("no trusted managed-session signer is configured")

    def verify(
        self,
        lease: SignedManagedSessionLease,
        *,
        now: datetime,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        del lease, now, access_token, refresh_token, expected_digest
        raise LeaseSignatureError("no trusted managed-session signer is configured")


class Ed25519SessionLeaseVerifier:
    """Verify detached Ed25519 signatures and token commitments.

    Trusted keys are raw 32-byte Ed25519 public keys keyed by the cloud
    ``key_id``.  Construction and every verification fail closed.
    """

    def __init__(self, public_keys: Mapping[str, bytes]) -> None:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError as error:  # pragma: no cover - deployment capability
            raise LeaseSignatureError(
                "Ed25519 verification capability is unavailable"
            ) from error
        if not public_keys:
            raise LeaseSignatureError("at least one managed-session signer is required")
        parsed: dict[str, object] = {}
        for key_id, value in public_keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("managed-session signer key_id is invalid")
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError("managed-session public keys must contain 32 raw bytes")
            parsed[key_id] = Ed25519PublicKey.from_public_bytes(value)
        self._keys = parsed
        self._invalid_signature = InvalidSignature

    def verify(
        self,
        lease: SignedManagedSessionLease,
        *,
        now: datetime,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        self.verify_identity(
            lease,
            access_token=access_token,
            refresh_token=refresh_token,
            expected_digest=expected_digest,
        )
        now = _utc_now(now)
        claims = lease.claims
        if now < claims.issued_at:
            raise LeaseValidationError("managed session lease is not active")
        if now >= claims.expires_at:
            raise LeaseValidationError("managed session lease has expired")
        return True

    def verify_identity(
        self,
        lease: SignedManagedSessionLease,
        *,
        access_token: str,
        refresh_token: str,
        expected_digest: str | None = None,
    ) -> bool:
        """Verify signed identity/token commitments without authorizing use.

        This is only for selecting the local read-only account partition after
        lease expiry.  Callers must never treat it as an authenticated session.
        """

        if not isinstance(lease, SignedManagedSessionLease):
            raise LeaseValidationError("managed session lease is invalid")
        actual_digest = lease.digest
        if expected_digest is not None and not hmac.compare_digest(
            actual_digest, str(expected_digest)
        ):
            raise LeaseValidationError("managed session lease digest does not match")
        signature = lease.signature
        public_key = self._keys.get(signature.key_id)
        if public_key is None:
            raise LeaseSignatureError("managed session signer is not trusted")
        try:
            detached = base64.b64decode(signature.value, validate=True)
            public_key.verify(detached, lease.canonical_payload())  # type: ignore[attr-defined]
        except (ValueError, self._invalid_signature) as error:
            raise LeaseSignatureError("managed session signature is invalid") from error
        claims = lease.claims
        actual_access = token_digest(access_token)
        actual_refresh = token_digest(refresh_token)
        if not hmac.compare_digest(actual_access, claims.access_token_sha256):
            raise LeaseValidationError("managed session access token does not match")
        if not hmac.compare_digest(actual_refresh, claims.refresh_token_sha256):
            raise LeaseValidationError("managed session refresh token does not match")
        return True


def require_verified(verdict: object) -> None:
    if verdict is not True:
        raise LeaseSignatureError(
            "managed session verifier must return the literal boolean true"
        )


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LeaseValidationError("verification time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "Ed25519SessionLeaseVerifier",
    "RejectingSessionLeaseVerifier",
    "SessionLeaseVerifier",
    "require_verified",
]

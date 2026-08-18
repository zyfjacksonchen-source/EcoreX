"""Control Plane adapter over the shared typed access-token verifier."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
from types import MappingProxyType
from typing import Any
from typing import Callable, Mapping

from ecorex.security import (
    AccessEntitlements,
    AccessTokenConfigurationError,
    Ed25519AccessTokenVerifier,
    VerifiedAccessClaims,
    parse_ed25519_public_keyring,
)

from .models import ControlPlaneAuthenticator, ControlPrincipal


# Backward-compatible public error name for existing deployment code.
ControlPlaneAuthenticationConfigurationError = AccessTokenConfigurationError


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SESSION_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SESSION_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SESSION_ROLES = frozenset({"TENANT_ADMIN", "AUDIT_ADMIN", "MEMBER"})
_SESSION_CLAIMS = {
    "schemaVersion",
    "iss",
    "aud",
    "sub",
    "sid",
    "tenantId",
    "roles",
    "weeklyTokenLimit",
    "iat",
    "nbf",
    "exp",
    "jti",
}


@dataclass(frozen=True, slots=True)
class Ed25519JWTAuthenticator(ControlPlaneAuthenticator):
    public_keys: Mapping[str, bytes]
    issuer: str
    audience: str
    max_token_lifetime_seconds: int = 900
    clock_skew_seconds: int = 30
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _verifier: Ed25519AccessTokenVerifier = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_verifier",
            Ed25519AccessTokenVerifier(
                public_keys=self.public_keys,
                issuer=self.issuer,
                audience=self.audience,
                max_token_lifetime_seconds=self.max_token_lifetime_seconds,
                clock_skew_seconds=self.clock_skew_seconds,
                clock=self.clock,
            ),
        )

    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        claims = self._verifier.verify(bearer_token)
        return ControlPrincipal(
            subject=claims.subject,
            client_id=claims.client_id,
            account_id=claims.account_id,
            organization_id=claims.organization_id,
            roles=claims.roles,
            token_id=claims.token_id,
        )


@dataclass(frozen=True, slots=True)
class EMateSessionJWTAuthenticator(ControlPlaneAuthenticator):
    """Verify current e-Mate access sessions for the Skill Hub only."""

    public_keys: Mapping[str, bytes]
    issuer: str
    audience: str
    max_token_lifetime_seconds: int = 900
    clock_skew_seconds: int = 60
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _invalid_signature: type[BaseException] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.issuer, str)
            or not 1 <= len(self.issuer) <= 256
            or any(ord(character) < 32 for character in self.issuer)
            or not isinstance(self.audience, str)
            or not 1 <= len(self.audience) <= 256
            or any(ord(character) < 32 for character in self.audience)
            or not 120 <= self.max_token_lifetime_seconds <= 24 * 60 * 60
            or not 0 <= self.clock_skew_seconds <= 120
        ):
            raise AccessTokenConfigurationError(
                "e-Mate session-token policy is invalid"
            )
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise AccessTokenConfigurationError(
                "Ed25519 verification is unavailable"
            ) from error
        parsed: dict[str, Any] = {}
        for key_id, material in self.public_keys.items():
            if (
                not isinstance(key_id, str)
                or _SESSION_ID.fullmatch(key_id) is None
                or not isinstance(material, bytes)
                or len(material) != 32
            ):
                raise AccessTokenConfigurationError(
                    "e-Mate session public-key configuration is invalid"
                )
            parsed[key_id] = Ed25519PublicKey.from_public_bytes(material)
        if not 1 <= len(parsed) <= 8:
            raise AccessTokenConfigurationError(
                "e-Mate session public-key configuration is invalid"
            )
        object.__setattr__(self, "public_keys", MappingProxyType(parsed))
        object.__setattr__(self, "_invalid_signature", InvalidSignature)

    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        try:
            if (
                not isinstance(bearer_token, str)
                or not 128 <= len(bearer_token) <= 4096
                or bearer_token.count(".") != 2
                or any(character.isspace() for character in bearer_token)
            ):
                raise ValueError
            encoded_header, encoded_claims, encoded_signature = bearer_token.split(".")
            header = _session_json_segment(encoded_header, maximum_bytes=1024)
            claims = _session_json_segment(encoded_claims, maximum_bytes=4096)
            if (
                set(header) != {"alg", "typ", "kid"}
                or header.get("alg") != "EdDSA"
                or header.get("typ") != "e-mate-auth-session+jwt"
            ):
                raise ValueError
            key_id = header.get("kid")
            if not isinstance(key_id, str) or _SESSION_ID.fullmatch(key_id) is None:
                raise ValueError
            key = self.public_keys.get(key_id)
            if key is None:
                raise ValueError
            signature = _session_b64url(encoded_signature, maximum_bytes=64)
            if len(signature) != 64:
                raise ValueError
            key.verify(
                signature,
                f"{encoded_header}.{encoded_claims}".encode("ascii"),
            )
            return self._project(claims)
        except (TypeError, ValueError, UnicodeError, self._invalid_signature):
            raise PermissionError("e-Mate session authentication failed") from None

    def _project(self, claims: dict[str, Any]) -> ControlPrincipal:
        if set(claims) != _SESSION_CLAIMS:
            raise ValueError
        subject = claims.get("sub")
        session_id = claims.get("sid")
        tenant_id = claims.get("tenantId")
        token_id = claims.get("jti")
        roles = claims.get("roles")
        issued_at = claims.get("iat")
        not_before = claims.get("nbf")
        expires_at = claims.get("exp")
        weekly_limit = claims.get("weeklyTokenLimit")
        if (
            claims.get("schemaVersion") != 1
            or claims.get("iss") != self.issuer
            or claims.get("aud") != self.audience
            or not isinstance(subject, str)
            or _SESSION_ID.fullmatch(subject) is None
            or not isinstance(session_id, str)
            or _SESSION_UUID.fullmatch(session_id) is None
            or not isinstance(tenant_id, str)
            or _SESSION_ID.fullmatch(tenant_id) is None
            or not isinstance(token_id, str)
            or _SESSION_UUID.fullmatch(token_id) is None
            or not isinstance(roles, list)
            or not 1 <= len(roles) <= 3
            or any(not isinstance(role, str) or role not in _SESSION_ROLES for role in roles)
            or len(set(roles)) != len(roles)
            or isinstance(weekly_limit, bool)
            or not isinstance(weekly_limit, int)
            or not 1 <= weekly_limit <= 2**53 - 1
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (issued_at, not_before, expires_at)
            )
        ):
            raise ValueError
        now_value = self.clock()
        if now_value.tzinfo is None or now_value.utcoffset() is None:
            raise ValueError
        now = int(now_value.astimezone(UTC).timestamp())
        if (
            issued_at > now + self.clock_skew_seconds
            or not_before > now + self.clock_skew_seconds
            or expires_at <= now - self.clock_skew_seconds
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_token_lifetime_seconds
        ):
            raise ValueError
        return ControlPrincipal(
            subject=subject,
            client_id=self.audience,
            account_id=subject,
            organization_id=tenant_id,
            roles=frozenset(roles),
            token_id=token_id,
        )


def _session_b64url(value: str, *, maximum_bytes: int) -> bytes:
    if not isinstance(value, str) or _SESSION_B64URL.fullmatch(value) is None:
        raise ValueError
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise ValueError from None
    if (
        not 1 <= len(decoded) <= maximum_bytes
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value
    ):
        raise ValueError
    return decoded


def _session_json_segment(value: str, *, maximum_bytes: int) -> dict[str, Any]:
    decoded = _session_b64url(value, maximum_bytes=maximum_bytes)
    try:
        parsed = json.loads(decoded, object_pairs_hook=_session_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError from None
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def _session_unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in items:
        if key in parsed:
            raise ValueError
        parsed[key] = value
    return parsed


__all__ = [
    "AccessEntitlements",
    "ControlPlaneAuthenticationConfigurationError",
    "Ed25519AccessTokenVerifier",
    "Ed25519JWTAuthenticator",
    "EMateSessionJWTAuthenticator",
    "VerifiedAccessClaims",
    "parse_ed25519_public_keyring",
]

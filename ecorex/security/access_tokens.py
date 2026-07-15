"""One fail-closed Ed25519 access-token verifier for EcoreX services.

The verifier never returns arbitrary JWT dictionaries.  It checks the compact
token and projects a bounded identity plus typed model/quota entitlements.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping


_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_QUOTA_PERIOD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class AccessTokenConfigurationError(RuntimeError):
    """Access-token trust configuration or policy is malformed."""


@dataclass(frozen=True, slots=True)
class AccessEntitlements:
    allowed_model_ids: frozenset[str] = frozenset()
    quota_period: str | None = None
    request_limit: int | None = None
    concurrent_request_limit: int | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAccessClaims:
    subject: str
    client_id: str
    account_id: str
    organization_id: str | None
    roles: frozenset[str]
    entitlements: AccessEntitlements


def parse_ed25519_public_keyring(value: str) -> dict[str, bytes]:
    """Parse bounded canonical JSON ``key id -> base64 public key`` data."""

    try:
        raw = json.loads(value, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AccessTokenConfigurationError(
            "access-token public-key configuration is invalid"
        ) from None
    if not isinstance(raw, dict) or not 1 <= len(raw) <= 16:
        raise AccessTokenConfigurationError(
            "access-token public-key configuration is invalid"
        )
    parsed: dict[str, bytes] = {}
    for key_id, encoded in raw.items():
        if (
            not isinstance(key_id, str)
            or _KEY_ID.fullmatch(key_id) is None
            or not isinstance(encoded, str)
            or len(encoded) > 128
        ):
            raise AccessTokenConfigurationError(
                "access-token public-key configuration is invalid"
            )
        try:
            material = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AccessTokenConfigurationError(
                "access-token public-key configuration is invalid"
            ) from None
        if len(material) != 32 or base64.b64encode(material).decode("ascii") != encoded:
            raise AccessTokenConfigurationError(
                "access-token public-key configuration is invalid"
            )
        parsed[key_id] = material
    return parsed


@dataclass(frozen=True, slots=True)
class Ed25519AccessTokenVerifier:
    public_keys: Mapping[str, bytes]
    issuer: str
    audience: str
    max_token_lifetime_seconds: int = 900
    clock_skew_seconds: int = 30
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _invalid_signature: type[BaseException] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.issuer, str)
            or not 1 <= len(self.issuer) <= 512
            or any(ord(character) < 32 for character in self.issuer)
            or not isinstance(self.audience, str)
            or not 1 <= len(self.audience) <= 256
            or any(ord(character) < 32 for character in self.audience)
            or not 60 <= self.max_token_lifetime_seconds <= 3600
            or not 0 <= self.clock_skew_seconds <= 120
        ):
            raise AccessTokenConfigurationError("access-token policy is invalid")
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
                or _KEY_ID.fullmatch(key_id) is None
                or not isinstance(material, bytes)
                or len(material) != 32
            ):
                raise AccessTokenConfigurationError(
                    "access-token public-key configuration is invalid"
                )
            parsed[key_id] = Ed25519PublicKey.from_public_bytes(material)
        if not parsed:
            raise AccessTokenConfigurationError(
                "access-token public-key configuration is missing"
            )
        object.__setattr__(self, "public_keys", MappingProxyType(parsed))
        object.__setattr__(self, "_invalid_signature", InvalidSignature)

    def verify(self, bearer_token: str) -> VerifiedAccessClaims:
        try:
            if (
                not isinstance(bearer_token, str)
                or not 128 <= len(bearer_token) <= 4096
                or bearer_token.count(".") != 2
                or any(character.isspace() for character in bearer_token)
            ):
                raise ValueError
            encoded_header, encoded_claims, encoded_signature = bearer_token.split(".")
            header = _json_segment(encoded_header, maximum_bytes=1024)
            claims = _json_segment(encoded_claims, maximum_bytes=8192)
            if set(header) != {"alg", "kid", "typ"} or (
                header.get("alg") != "EdDSA" or header.get("typ") != "JWT"
            ):
                raise ValueError
            key_id = header.get("kid")
            if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
                raise ValueError
            public_key = self.public_keys.get(key_id)
            if public_key is None:
                raise ValueError
            signature = _decode_b64url(encoded_signature, maximum_bytes=64)
            if len(signature) != 64:
                raise ValueError
            public_key.verify(
                signature,
                f"{encoded_header}.{encoded_claims}".encode("ascii"),
            )
            return self._project(claims)
        except (KeyError, TypeError, ValueError, UnicodeError, self._invalid_signature):
            raise PermissionError("access-token authentication failed") from None

    def _project(self, claims: dict[str, Any]) -> VerifiedAccessClaims:
        issued_at = _integer_claim(claims, "iat")
        expires_at = _integer_claim(claims, "exp")
        not_before = claims.get("nbf", issued_at)
        if isinstance(not_before, bool) or not isinstance(not_before, int):
            raise ValueError
        now_value = self.clock()
        if now_value.tzinfo is None or now_value.utcoffset() is None:
            raise ValueError
        now = int(now_value.astimezone(UTC).timestamp())
        audience = claims.get("aud")
        audience_valid = audience == self.audience or (
            isinstance(audience, list)
            and 1 <= len(audience) <= 8
            and self.audience in audience
            and all(isinstance(item, str) for item in audience)
        )
        if (
            claims.get("iss") != self.issuer
            or not audience_valid
            or claims.get("token_use") != "access"
            or issued_at > now + self.clock_skew_seconds
            or not_before > now + self.clock_skew_seconds
            or expires_at <= now - self.clock_skew_seconds
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_token_lifetime_seconds
        ):
            raise ValueError
        organization = claims.get("organization_id")
        if organization is not None and (
            not isinstance(organization, str)
            or _IDENTITY.fullmatch(organization) is None
        ):
            raise ValueError
        roles_raw = claims.get("roles", [])
        if (
            not isinstance(roles_raw, list)
            or len(roles_raw) > 32
            or any(
                not isinstance(role, str) or _ROLE.fullmatch(role) is None
                for role in roles_raw
            )
            or len(set(roles_raw)) != len(roles_raw)
        ):
            raise ValueError
        return VerifiedAccessClaims(
            subject=_identity_claim(claims, "sub"),
            client_id=_identity_claim(claims, "client_id"),
            account_id=_identity_claim(claims, "account_id"),
            organization_id=organization,
            roles=frozenset(roles_raw),
            entitlements=_entitlements(claims),
        )


def _entitlements(claims: Mapping[str, Any]) -> AccessEntitlements:
    models_raw = claims.get("allowed_model_ids", [])
    if (
        not isinstance(models_raw, list)
        or len(models_raw) > 128
        or any(
            not isinstance(model, str) or _MODEL.fullmatch(model) is None
            for model in models_raw
        )
        or len(models_raw) != len(set(models_raw))
    ):
        raise ValueError
    quota_period = claims.get("quota_period")
    if quota_period is not None and (
        not isinstance(quota_period, str)
        or _QUOTA_PERIOD.fullmatch(quota_period) is None
    ):
        raise ValueError
    request_limit = claims.get("request_limit")
    if request_limit is not None and (
        isinstance(request_limit, bool)
        or not isinstance(request_limit, int)
        or not 1 <= request_limit <= 1_000_000
    ):
        raise ValueError
    concurrent_limit = claims.get("concurrent_request_limit")
    if concurrent_limit is not None and (
        isinstance(concurrent_limit, bool)
        or not isinstance(concurrent_limit, int)
        or not 1 <= concurrent_limit <= 1_000
    ):
        raise ValueError
    return AccessEntitlements(
        allowed_model_ids=frozenset(models_raw),
        quota_period=quota_period,
        request_limit=request_limit,
        concurrent_request_limit=concurrent_limit,
    )


def _identity_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError
    return value


def _integer_claim(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _json_segment(segment: str, *, maximum_bytes: int) -> dict[str, Any]:
    decoded = _decode_b64url(segment, maximum_bytes=maximum_bytes)
    try:
        value = json.loads(decoded.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError from None
    if not isinstance(value, dict):
        raise ValueError
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _decode_b64url(segment: str, *, maximum_bytes: int) -> bytes:
    if (
        not isinstance(segment, str)
        or not segment
        or len(segment) > maximum_bytes * 2
        or _B64URL.fullmatch(segment) is None
        or "=" in segment
    ):
        raise ValueError
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, binascii.Error):
        raise ValueError from None
    if (
        len(decoded) > maximum_bytes
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != segment
    ):
        raise ValueError
    return decoded


__all__ = [
    "AccessEntitlements",
    "AccessTokenConfigurationError",
    "Ed25519AccessTokenVerifier",
    "VerifiedAccessClaims",
    "parse_ed25519_public_keyring",
]

"""Typed account/model authorization for the production Image service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import re
from typing import Callable, Mapping

from ecorex.security import (
    AccessTokenConfigurationError,
    Ed25519AccessTokenVerifier,
    parse_ed25519_public_keyring as _parse_shared_keyring,
)


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{2,255}$")
_PERIOD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ImageAuthenticationConfigurationError(RuntimeError):
    """Image authentication trust or entitlement policy is invalid."""


def parse_ed25519_public_keyring(value: str) -> dict[str, bytes]:
    """Parse the shared canonical keyring with an Image-domain error."""

    try:
        return _parse_shared_keyring(value)
    except AccessTokenConfigurationError:
        raise ImageAuthenticationConfigurationError(
            "image public-key configuration is invalid"
        ) from None


@dataclass(frozen=True, slots=True)
class ImageProductionPrincipal:
    """Only the verified identity and signed Image entitlements reach routes."""

    subject: str
    account_id: str
    allowed_model_ids: frozenset[str]
    quota_period: str
    request_limit: int
    concurrent_request_limit: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject, str)
            or _IDENTITY.fullmatch(self.subject) is None
            or not isinstance(self.account_id, str)
            or _IDENTITY.fullmatch(self.account_id) is None
            or not isinstance(self.quota_period, str)
            or _PERIOD.fullmatch(self.quota_period) is None
        ):
            raise ValueError("image principal identity is incomplete")
        if (
            not isinstance(self.allowed_model_ids, frozenset)
            or not self.allowed_model_ids
            or len(self.allowed_model_ids) > 128
            or any(_MODEL.fullmatch(model_id) is None for model_id in self.allowed_model_ids)
        ):
            raise ValueError("image principal model entitlement is invalid")
        if (
            isinstance(self.request_limit, bool)
            or not isinstance(self.request_limit, int)
            or not 1 <= self.request_limit <= 1_000_000
            or isinstance(self.concurrent_request_limit, bool)
            or not isinstance(self.concurrent_request_limit, int)
            or not 1 <= self.concurrent_request_limit <= 1_000
        ):
            raise ValueError("image principal quota entitlement is invalid")


@dataclass(frozen=True, slots=True)
class Ed25519ImageJWTAuthenticator:
    """Verify one short-lived token and intersect it with service policy."""

    public_keys: Mapping[str, bytes]
    issuer: str
    audience: str
    service_model_ids: frozenset[str]
    max_token_lifetime_seconds: int = 900
    clock_skew_seconds: int = 30
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _verifier: Ed25519AccessTokenVerifier = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.service_model_ids, frozenset)
            or not self.service_model_ids
            or len(self.service_model_ids) > 128
            or any(_MODEL.fullmatch(model_id) is None for model_id in self.service_model_ids)
        ):
            raise ImageAuthenticationConfigurationError(
                "image service model policy is invalid"
            )
        try:
            verifier = Ed25519AccessTokenVerifier(
                public_keys=self.public_keys,
                issuer=self.issuer,
                audience=self.audience,
                max_token_lifetime_seconds=self.max_token_lifetime_seconds,
                clock_skew_seconds=self.clock_skew_seconds,
                clock=self.clock,
            )
        except AccessTokenConfigurationError:
            raise ImageAuthenticationConfigurationError(
                "image token policy is invalid"
            ) from None
        object.__setattr__(self, "_verifier", verifier)

    def authenticate(self, bearer_token: str) -> ImageProductionPrincipal:
        claims = self._verifier.verify(bearer_token)
        entitlement = claims.entitlements
        if (
            entitlement.quota_period is None
            or entitlement.request_limit is None
            or entitlement.concurrent_request_limit is None
        ):
            raise PermissionError("image entitlements are incomplete")
        allowed = entitlement.allowed_model_ids & self.service_model_ids
        if not allowed:
            raise PermissionError("image model entitlement is empty")
        try:
            return ImageProductionPrincipal(
                subject=claims.subject,
                account_id=claims.account_id,
                allowed_model_ids=allowed,
                quota_period=entitlement.quota_period,
                request_limit=entitlement.request_limit,
                concurrent_request_limit=entitlement.concurrent_request_limit,
            )
        except ValueError:
            # A signature-valid identity can still be outside the narrower
            # Image tenant namespace.  Authentication failures must never
            # escape as a route-level 500 or expose the rejected claim.
            raise PermissionError("image principal is not authorized") from None


__all__ = [
    "Ed25519ImageJWTAuthenticator",
    "ImageAuthenticationConfigurationError",
    "ImageProductionPrincipal",
    "parse_ed25519_public_keyring",
]

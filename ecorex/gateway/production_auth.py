"""Gateway projection over the shared typed Ed25519 access verifier."""

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

from .server import GatewayPrincipal


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class GatewayAuthenticationConfigurationError(RuntimeError):
    """Gateway authentication trust or entitlement policy is invalid."""


def parse_ed25519_public_keyring(value: str) -> dict[str, bytes]:
    """Parse the shared canonical public-key format with gateway errors."""

    try:
        return _parse_shared_keyring(value)
    except AccessTokenConfigurationError:
        raise GatewayAuthenticationConfigurationError(
            "gateway public-key configuration is invalid"
        ) from None


@dataclass(frozen=True, slots=True)
class Ed25519GatewayJWTAuthenticator:
    """Require typed model/quota entitlements and emit a GatewayPrincipal."""

    public_keys: Mapping[str, bytes]
    issuer: str
    audience: str
    service_model_ids: frozenset[str] | None
    access_token_is_current: Callable[[str, str], bool | None] | None = None
    max_token_lifetime_seconds: int = 900
    clock_skew_seconds: int = 30
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _verifier: Ed25519AccessTokenVerifier = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.service_model_ids is not None and (
            not self.service_model_ids
            or len(self.service_model_ids) > 128
            or any(_MODEL_ID.fullmatch(item) is None for item in self.service_model_ids)
        ):
            raise GatewayAuthenticationConfigurationError(
                "gateway model policy is invalid"
            )
        if self.access_token_is_current is not None and not callable(
            self.access_token_is_current
        ):
            raise GatewayAuthenticationConfigurationError(
                "gateway token authority is invalid"
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
            raise GatewayAuthenticationConfigurationError(
                "gateway token policy is invalid"
            ) from None
        object.__setattr__(self, "_verifier", verifier)

    def authenticate(self, bearer_token: str) -> GatewayPrincipal:
        claims = self._verifier.verify(bearer_token)
        if self.access_token_is_current is not None and (
            claims.token_id is None
            or self.access_token_is_current(claims.account_id, claims.token_id) is not True
        ):
            raise PermissionError("gateway access token is no longer current")
        entitlement = claims.entitlements
        if (
            entitlement.quota_period is None
            or entitlement.request_limit is None
            or entitlement.concurrent_request_limit is None
        ):
            raise PermissionError("gateway entitlements are incomplete")
        allowed = entitlement.allowed_model_ids
        if self.service_model_ids is not None:
            allowed &= self.service_model_ids
        if not allowed:
            raise PermissionError("gateway model entitlement is empty")
        if any(
            not 1 <= len(value) <= 128
            or any(character.isspace() or ord(character) < 32 for character in value)
            for value in (claims.subject, claims.account_id, entitlement.quota_period)
        ):
            raise PermissionError("gateway identity entitlement is incompatible")
        try:
            return GatewayPrincipal(
                subject=claims.subject,
                account_id=claims.account_id,
                organization_id=claims.organization_id,
                allowed_model_ids=allowed,
                quota_period=entitlement.quota_period,
                request_limit=entitlement.request_limit,
                concurrent_request_limit=entitlement.concurrent_request_limit,
            )
        except (TypeError, ValueError):
            raise PermissionError("gateway principal is incompatible") from None


__all__ = [
    "Ed25519GatewayJWTAuthenticator",
    "GatewayAuthenticationConfigurationError",
    "parse_ed25519_public_keyring",
]

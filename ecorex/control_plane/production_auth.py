"""Control Plane adapter over the shared typed access-token verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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


__all__ = [
    "AccessEntitlements",
    "ControlPlaneAuthenticationConfigurationError",
    "Ed25519AccessTokenVerifier",
    "Ed25519JWTAuthenticator",
    "VerifiedAccessClaims",
    "parse_ed25519_public_keyring",
]

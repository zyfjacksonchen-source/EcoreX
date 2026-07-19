"""Signed managed-session contracts exposed to the local Runtime.

The cloud lease is the authority.  Local code may project these claims, but it
must never mint or extend them.  Tokens are represented only by SHA-256
commitments in this module; plaintext token material belongs in a
``CredentialVault``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


MAX_LEASE_DURATION = timedelta(hours=72)
_LEASE_DOMAIN = b"ecorex-managed-session-lease-v1\n"
_DOCUMENT_DOMAIN = b"ecorex-managed-session-document-v1\n"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManagedSessionError(RuntimeError):
    """Base class for fail-closed managed-session failures."""


class LeaseValidationError(ManagedSessionError):
    pass


class LeaseSignatureError(LeaseValidationError):
    pass


class SessionUnavailable(ManagedSessionError):
    pass


class SessionConflict(ManagedSessionError):
    pass


class StaleSessionRequest(SessionConflict):
    pass


class SessionRestartRequired(SessionConflict):
    pass


class SessionVaultError(SessionUnavailable):
    pass


def _required_id(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise LeaseValidationError(f"{label} is invalid")
    return normalized


def _required_text(value: object, label: str, *, maximum: int = 256) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise LeaseValidationError(f"{label} is invalid")
    return normalized


def _sha256(value: object, label: str) -> str:
    normalized = str(value or "").casefold()
    if not _HEX_SHA256.fullmatch(normalized):
        raise LeaseValidationError(f"{label} is invalid")
    return normalized


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LeaseValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise LeaseValidationError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LeaseValidationError(f"{label} is invalid") from None
    return _utc(parsed, label)


def _string_set(
    values: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 128,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise LeaseValidationError(f"{label} is invalid")
    normalized = tuple(
        sorted({_required_text(item, label, maximum=256) for item in values})
    )
    if not minimum <= len(normalized) <= maximum:
        raise LeaseValidationError(f"{label} is invalid")
    return normalized


def _quota(value: object) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or len(value) > 128:
        raise LeaseValidationError("quota is invalid")
    normalized: dict[str, int] = {}
    for raw_name, raw_limit in value.items():
        name = _required_text(raw_name, "quota name", maximum=128)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise LeaseValidationError("quota limit is invalid")
        if raw_limit < 0 or raw_limit > 10**15:
            raise LeaseValidationError("quota limit is invalid")
        normalized[name] = raw_limit
    return MappingProxyType(dict(sorted(normalized.items())))


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise LeaseValidationError("lease cannot be canonically encoded") from None


@dataclass(frozen=True, slots=True)
class SessionLeaseSignature:
    algorithm: str
    key_id: str
    value: str

    def __post_init__(self) -> None:
        if self.algorithm != "ed25519":
            raise LeaseValidationError("managed session signatures must use ed25519")
        key_id = str(self.key_id or "")
        if not _SAFE_KEY_ID.fullmatch(key_id):
            raise LeaseValidationError("lease signature key_id is invalid")
        object.__setattr__(self, "key_id", key_id)
        value = str(self.value or "")
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError:
            raise LeaseValidationError("lease signature is invalid") from None
        if len(decoded) != 64:
            raise LeaseValidationError("lease signature is invalid")
        object.__setattr__(self, "value", value)

    def to_dict(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SessionLeaseSignature":
        if not isinstance(raw, Mapping) or set(raw) != {"algorithm", "key_id", "value"}:
            raise LeaseValidationError("lease signature envelope is invalid")
        return cls(
            algorithm=str(raw["algorithm"]),
            key_id=str(raw["key_id"]),
            value=str(raw["value"]),
        )


@dataclass(frozen=True, slots=True)
class ManagedSessionLeaseClaims:
    lease_id: str
    account_id: str
    organization_id: str
    display_name: str
    roles: tuple[str, ...]
    model_allowlist: tuple[str, ...]
    quota: Mapping[str, int]
    admin_denies: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    revision: int
    access_token_sha256: str
    refresh_token_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise LeaseValidationError("unsupported managed session lease schema")
        object.__setattr__(self, "lease_id", _required_id(self.lease_id, "lease_id"))
        object.__setattr__(
            self, "account_id", _required_id(self.account_id, "account_id")
        )
        object.__setattr__(
            self,
            "organization_id",
            _required_id(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "display_name",
            _required_text(self.display_name, "display_name", maximum=256),
        )
        object.__setattr__(
            self,
            "roles",
            _string_set(self.roles, "roles", minimum=1, maximum=64),
        )
        object.__setattr__(
            self,
            "model_allowlist",
            _string_set(
                self.model_allowlist,
                "model_allowlist",
                minimum=1,
                maximum=256,
            ),
        )
        object.__setattr__(self, "quota", _quota(self.quota))
        object.__setattr__(
            self,
            "admin_denies",
            _string_set(self.admin_denies, "admin_denies", maximum=256),
        )
        issued_at = _utc(self.issued_at, "issued_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at <= issued_at or expires_at - issued_at > MAX_LEASE_DURATION:
            raise LeaseValidationError("managed session lease duration is invalid")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise LeaseValidationError("lease revision is invalid")
        if self.revision <= 0 or self.revision > 2**63 - 1:
            raise LeaseValidationError("lease revision is invalid")
        object.__setattr__(
            self,
            "access_token_sha256",
            _sha256(self.access_token_sha256, "access token digest"),
        )
        object.__setattr__(
            self,
            "refresh_token_sha256",
            _sha256(self.refresh_token_sha256, "refresh token digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lease_id": self.lease_id,
            "account_id": self.account_id,
            "organization_id": self.organization_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "model_allowlist": list(self.model_allowlist),
            "quota": dict(self.quota),
            "admin_denies": list(self.admin_denies),
            "issued_at": _format_time(self.issued_at),
            "expires_at": _format_time(self.expires_at),
            "revision": self.revision,
            "access_token_sha256": self.access_token_sha256,
            "refresh_token_sha256": self.refresh_token_sha256,
        }

    def canonical_payload(self) -> bytes:
        return _LEASE_DOMAIN + _canonical_json(self.to_dict()) + b"\n"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ManagedSessionLeaseClaims":
        expected = {
            "schema_version",
            "lease_id",
            "account_id",
            "organization_id",
            "display_name",
            "roles",
            "model_allowlist",
            "quota",
            "admin_denies",
            "issued_at",
            "expires_at",
            "revision",
            "access_token_sha256",
            "refresh_token_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise LeaseValidationError("managed session lease claims are invalid")
        return cls(
            schema_version=raw["schema_version"],
            lease_id=raw["lease_id"],
            account_id=raw["account_id"],
            organization_id=raw["organization_id"],
            display_name=raw["display_name"],
            roles=raw["roles"],
            model_allowlist=raw["model_allowlist"],
            quota=raw["quota"],
            admin_denies=raw["admin_denies"],
            issued_at=_parse_time(raw["issued_at"], "issued_at"),
            expires_at=_parse_time(raw["expires_at"], "expires_at"),
            revision=raw["revision"],
            access_token_sha256=raw["access_token_sha256"],
            refresh_token_sha256=raw["refresh_token_sha256"],
        )


@dataclass(frozen=True, slots=True)
class SignedManagedSessionLease:
    claims: ManagedSessionLeaseClaims
    signature: SessionLeaseSignature

    def __post_init__(self) -> None:
        if not isinstance(self.claims, ManagedSessionLeaseClaims):
            raise LeaseValidationError("managed session lease claims are invalid")
        if not isinstance(self.signature, SessionLeaseSignature):
            raise LeaseValidationError("managed session lease signature is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"claims": self.claims.to_dict(), "signature": self.signature.to_dict()}

    def canonical_payload(self) -> bytes:
        return self.claims.canonical_payload()

    def canonical_document(self) -> bytes:
        return _DOCUMENT_DOMAIN + _canonical_json(self.to_dict()) + b"\n"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_document()).hexdigest()

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SignedManagedSessionLease":
        if not isinstance(raw, Mapping) or set(raw) != {"claims", "signature"}:
            raise LeaseValidationError("signed managed session lease is invalid")
        claims = raw["claims"]
        signature = raw["signature"]
        if not isinstance(claims, Mapping) or not isinstance(signature, Mapping):
            raise LeaseValidationError("signed managed session lease is invalid")
        return cls(
            claims=ManagedSessionLeaseClaims.from_dict(claims),
            signature=SessionLeaseSignature.from_dict(signature),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "SignedManagedSessionLease":
        if isinstance(payload, str):
            encoded = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            encoded = payload
        else:
            raise LeaseValidationError("signed managed session lease is invalid")
        if not encoded or len(encoded) > 128 * 1024:
            raise LeaseValidationError("signed managed session lease is invalid")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LeaseValidationError(
                "signed managed session lease is invalid"
            ) from None
        if not isinstance(raw, Mapping):
            raise LeaseValidationError("signed managed session lease is invalid")
        return cls.from_dict(raw)


@dataclass(frozen=True, slots=True)
class ManagedSessionSnapshot:
    generation: int
    lease_digest: str
    lease_id: str
    account_id: str
    organization_id: str
    display_name: str
    roles: tuple[str, ...]
    model_allowlist: tuple[str, ...]
    quota: Mapping[str, int]
    admin_denies: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "model_allowlist", tuple(self.model_allowlist))
        object.__setattr__(self, "quota", MappingProxyType(dict(self.quota)))
        object.__setattr__(self, "admin_denies", tuple(self.admin_denies))

    @classmethod
    def from_lease(
        cls,
        lease: SignedManagedSessionLease,
        *,
        generation: int,
    ) -> "ManagedSessionSnapshot":
        claims = lease.claims
        return cls(
            generation=generation,
            lease_digest=lease.digest,
            lease_id=claims.lease_id,
            account_id=claims.account_id,
            organization_id=claims.organization_id,
            display_name=claims.display_name,
            roles=claims.roles,
            model_allowlist=claims.model_allowlist,
            quota=claims.quota,
            admin_denies=claims.admin_denies,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
            revision=claims.revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "lease_digest": self.lease_digest,
            "lease_id": self.lease_id,
            "account_id": self.account_id,
            "organization_id": self.organization_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "model_allowlist": list(self.model_allowlist),
            "quota": dict(self.quota),
            "admin_denies": list(self.admin_denies),
            "issued_at": _format_time(self.issued_at),
            "expires_at": _format_time(self.expires_at),
            "revision": self.revision,
        }

    @property
    def allowed_model_ids(self) -> tuple[str, ...]:
        return self.model_allowlist


@dataclass(frozen=True, slots=True)
class SessionLogoutReceipt:
    generation: int
    client_request_hash: str
    already_applied: bool


@dataclass(frozen=True, slots=True)
class SessionRecoveryReport:
    finalized_installs: int = 0
    aborted_installs: int = 0
    cleaned_credentials: int = 0
    blocked_operations: int = 0


@dataclass(frozen=True, slots=True)
class SessionRefreshContext:
    lease: SignedManagedSessionLease
    access_expires_at: datetime
    refresh_token: str

    def __repr__(self) -> str:
        return (
            "<SessionRefreshContext "
            f"lease_id={self.lease.claims.lease_id!r} "
            f"access_expires_at={self.access_expires_at.isoformat()!r} "
            "refresh_token=<redacted>>"
        )


@dataclass(frozen=True, slots=True)
class SessionRevocationContext:
    lease: SignedManagedSessionLease
    refresh_token: str

    def __repr__(self) -> str:
        return (
            "<SessionRevocationContext "
            f"lease_id={self.lease.claims.lease_id!r} "
            "refresh_token=<redacted>>"
        )


@dataclass(frozen=True, slots=True)
class SessionAuditRecord:
    sequence: int
    event_type: str
    outcome: str
    reason_code: str | None
    client_request_hash: str | None
    account_hash: str | None
    organization_hash: str | None
    lease_digest: str | None
    revision: int | None
    generation: int
    details: Mapping[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not token or "\x00" in token:
        raise LeaseValidationError("token material is invalid")
    encoded = token.encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise LeaseValidationError("token material is invalid")
    return hashlib.sha256(encoded).hexdigest()


def redacted_hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"ecorex-session:{kind}:{value}".encode("utf-8")).hexdigest()


__all__ = [
    "LeaseSignatureError",
    "LeaseValidationError",
    "MAX_LEASE_DURATION",
    "ManagedSessionError",
    "ManagedSessionLeaseClaims",
    "ManagedSessionSnapshot",
    "SessionAuditRecord",
    "SessionConflict",
    "SessionLeaseSignature",
    "SessionLogoutReceipt",
    "SessionRecoveryReport",
    "SessionRefreshContext",
    "SessionRevocationContext",
    "SessionRestartRequired",
    "SessionUnavailable",
    "SessionVaultError",
    "SignedManagedSessionLease",
    "StaleSessionRequest",
    "redacted_hash",
    "token_digest",
]

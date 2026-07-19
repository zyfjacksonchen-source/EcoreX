"""Cloud authority for durable EcoreX managed-device authorization.

Only commitments and signed public claims are persisted. Device, access,
refresh and legacy credentials are derived or verified in memory and never
enter SQLite, argv, receipts or exception text.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ecorex.release.signing import ReleaseSigner, SigningError
from ecorex.session.models import (
    MAX_LEASE_DURATION,
    ManagedSessionLeaseClaims,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
)

from .device_identity_schema import DeviceIdentitySchemaManager


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_FLOW_ID = re.compile(r"^dif_[0-9a-f]{32}$")
_SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
_USER_CODE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
_TOKEN_TTL = timedelta(minutes=15)
_FLOW_TTL = timedelta(minutes=15)
_POLL_INTERVAL_SECONDS = 5
_MAX_LEGACY_ATTEMPTS = 5


class DeviceIdentityError(RuntimeError):
    code = "device_identity_error"


class DeviceIdentityNotFound(DeviceIdentityError):
    code = "device_identity_not_found"


class DeviceIdentityConflict(DeviceIdentityError):
    code = "device_identity_conflict"


class DeviceIdentityUnauthorized(DeviceIdentityError):
    code = "device_identity_unauthorized"


class DeviceRefreshRequired(DeviceIdentityUnauthorized):
    code = "invalid_grant"


class DeviceIdentityUnavailable(DeviceIdentityError):
    code = "device_identity_unavailable"


@dataclass(frozen=True, slots=True)
class DeviceIdentitySecrets:
    derivation_key: bytes
    legacy_credential_pepper: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.derivation_key, bytes)
            or len(self.derivation_key) < 32
            or len(self.derivation_key) > 64
            or not isinstance(self.legacy_credential_pepper, bytes)
            or len(self.legacy_credential_pepper) < 32
            or len(self.legacy_credential_pepper) > 64
            or hmac.compare_digest(self.derivation_key, self.legacy_credential_pepper)
        ):
            raise ValueError("device identity secret configuration is invalid")

    def __repr__(self) -> str:
        return "<DeviceIdentitySecrets derivation=<redacted> legacy=<redacted>>"


@dataclass(frozen=True, slots=True)
class DeviceAccountIdentity:
    account_id: str
    organization_id: str
    display_name: str
    roles: tuple[str, ...]
    model_allowlist: tuple[str, ...]
    quota: Mapping[str, int]
    admin_denies: tuple[str, ...] = ()
    auth_epoch: int = 0

    def __post_init__(self) -> None:
        # Reuse the managed-session claim validator with harmless commitments.
        ManagedSessionLeaseClaims(
            lease_id="identity-validation",
            account_id=self.account_id,
            organization_id=self.organization_id,
            display_name=self.display_name,
            roles=self.roles,
            model_allowlist=self.model_allowlist,
            quota=self.quota,
            admin_denies=self.admin_denies,
            issued_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC) + timedelta(seconds=1),
            revision=1,
            access_token_sha256="0" * 64,
            refresh_token_sha256="0" * 64,
        )
        if not isinstance(self.auth_epoch, int) or self.auth_epoch < 0:
            raise ValueError("device account auth epoch is invalid")


@runtime_checkable
class DeviceAccountDirectory(Protocol):
    def resolve(self, account_id: str) -> DeviceAccountIdentity: ...


@dataclass(frozen=True, slots=True)
class DeviceChallenge:
    provider_flow_id: str
    device_code: str
    user_code: str
    verification_url: str
    expires_at: datetime
    poll_interval_seconds: int = _POLL_INTERVAL_SECONDS

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider_flow_id": self.provider_flow_id,
            "device_code": self.device_code,
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "expires_at": _iso(self.expires_at),
            "poll_interval_seconds": self.poll_interval_seconds,
        }


@dataclass(frozen=True, slots=True)
class DeviceTokenResult:
    status: str
    retry_after_seconds: int | None = None
    lease: SignedManagedSessionLease | None = None
    access_token: str | None = None
    refresh_token: str | None = None

    def to_dict(self) -> dict[str, object]:
        if self.status == "authorized":
            assert self.lease and self.access_token and self.refresh_token
            return {
                "schema_version": 1,
                "status": self.status,
                "lease": self.lease.to_dict(),
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
            }
        return {
            "schema_version": 1,
            "status": self.status,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class DeviceRevocationResult:
    lease_id: str
    account_id: str
    already_revoked: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "revoked",
            "lease_id": self.lease_id,
            "account_id": self.account_id,
            "already_revoked": self.already_revoked,
        }


Clock = Callable[[], datetime]


def _clock() -> datetime:
    return datetime.now(UTC)


class ManagedDeviceIdentityBroker:
    """Single-node durable broker behind the strict Runtime HTTPS client."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        account_directory: DeviceAccountDirectory,
        access_signer: ReleaseSigner,
        lease_signer: ReleaseSigner,
        secrets: DeviceIdentitySecrets,
        issuer: str,
        audience: str,
        verification_url: str,
        allowed_client_ids: frozenset[str],
        clock: Clock = _clock,
        initialize: bool = True,
    ) -> None:
        self.path = Path(database_path).resolve()
        self.account_directory = account_directory
        self.access_signer = access_signer
        self.lease_signer = lease_signer
        self.secrets = secrets
        self.issuer = _bounded_text(issuer, "device identity issuer", 512)
        self.audience = _bounded_text(audience, "device identity audience", 256)
        self.verification_url = _https_url(verification_url)
        verification_path = urlsplit(self.verification_url).path
        if verification_path in {"", "/"} or verification_path.startswith(
            "/v1/device/"
        ):
            raise ValueError("device verification URL path is invalid")
        clients = frozenset(str(value) for value in allowed_client_ids)
        if not clients or any(_CLIENT_ID.fullmatch(value) is None for value in clients):
            raise ValueError("device identity client allowlist is invalid")
        if access_signer.key_id == lease_signer.key_id:
            raise ValueError(
                "access and managed-session signing roles must be distinct"
            )
        self.allowed_client_ids = clients
        self.clock = clock
        self._approval_lock = threading.Lock()
        if initialize:
            DeviceIdentitySchemaManager(self.path).migrate()
        else:
            DeviceIdentitySchemaManager(self.path).validate()

    def begin(self, *, client_id: str, idempotency_key: str) -> DeviceChallenge:
        self._client(client_id)
        self._idempotency(idempotency_key)
        now = self._now()
        request_hash = self._commitment("begin", f"{client_id}\0{idempotency_key}")
        request_digest = hashlib.sha256(
            _canonical({"schema_version": 1, "client_id": client_id})
        ).hexdigest()
        connection = self._connect()
        try:
            existing = connection.execute(
                "SELECT * FROM device_identity_flows WHERE begin_request_hash=?",
                (request_hash,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise DeviceIdentityConflict(
                        "device begin request identity changed"
                    )
                return self._challenge(existing)
            for _attempt in range(8):
                flow_id = "dif_" + secrets.token_hex(16)
                device_code = self._derived_token("device-code", flow_id, prefix="dc_")
                user_code = self._user_code(flow_id)
                expires_at = now + _FLOW_TTL
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO device_identity_flows("
                        "flow_id,client_id,begin_request_hash,request_digest,"
                        "device_code_digest,user_code_digest,status,created_at,"
                        "expires_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                        (
                            flow_id,
                            client_id,
                            request_hash,
                            request_digest,
                            self._commitment("device-code", device_code),
                            self._commitment("user-code", user_code),
                            _iso(now),
                            _iso(expires_at),
                            _iso(now),
                        ),
                    )
                    self._audit(
                        connection,
                        "device.authorize.started",
                        "accepted",
                        flow_id=flow_id,
                        details={"client_id": client_id},
                        now=now,
                    )
                    connection.commit()
                    return DeviceChallenge(
                        provider_flow_id=flow_id,
                        device_code=device_code,
                        user_code=user_code,
                        verification_url=self.verification_url,
                        expires_at=expires_at,
                    )
                except sqlite3.IntegrityError:
                    connection.rollback()
                    concurrent = connection.execute(
                        "SELECT * FROM device_identity_flows WHERE begin_request_hash=?",
                        (request_hash,),
                    ).fetchone()
                    if concurrent is not None:
                        if concurrent["request_digest"] != request_digest:
                            raise DeviceIdentityConflict(
                                "device begin request identity changed"
                            )
                        return self._challenge(concurrent)
            raise DeviceIdentityUnavailable("device authorization identity unavailable")
        finally:
            connection.close()

    def poll(
        self,
        *,
        client_id: str,
        provider_flow_id: str,
        device_code: str,
        idempotency_key: str,
    ) -> DeviceTokenResult:
        self._client(client_id)
        self._idempotency(idempotency_key)
        if _FLOW_ID.fullmatch(provider_flow_id) is None:
            raise DeviceIdentityUnauthorized("device authorization failed")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM device_identity_flows WHERE flow_id=?",
                (provider_flow_id,),
            ).fetchone()
            if (
                row is None
                or row["client_id"] != client_id
                or not hmac.compare_digest(
                    str(row["device_code_digest"]),
                    self._commitment("device-code", device_code),
                )
            ):
                connection.rollback()
                raise DeviceIdentityUnauthorized("device authorization failed")
            if _time(row["expires_at"]) <= now and row["status"] == "pending":
                connection.execute(
                    "UPDATE device_identity_flows SET status='expired',updated_at=? "
                    "WHERE flow_id=? AND status='pending'",
                    (_iso(now), provider_flow_id),
                )
                self._audit(
                    connection,
                    "device.token.expired",
                    "expired",
                    flow_id=provider_flow_id,
                    details={},
                    now=now,
                )
                connection.commit()
                return DeviceTokenResult(status="expired", retry_after_seconds=None)
            if row["status"] in {"denied", "expired"}:
                connection.rollback()
                return DeviceTokenResult(status=str(row["status"]))
            last_poll = _time(row["last_polled_at"]) if row["last_polled_at"] else None
            slow = last_poll is not None and (now - last_poll).total_seconds() < 4
            connection.execute(
                "UPDATE device_identity_flows SET poll_attempts=poll_attempts+1,"
                "last_polled_at=?,updated_at=? WHERE flow_id=?",
                (_iso(now), _iso(now), provider_flow_id),
            )
            connection.commit()
            if row["status"] == "pending":
                return DeviceTokenResult(
                    status="slow_down" if slow else "pending",
                    retry_after_seconds=10 if slow else _POLL_INTERVAL_SECONDS,
                )
        finally:
            connection.close()
        return self._grant(provider_flow_id, client_id=client_id)

    def approve(self, *, user_code: str, account_id: str) -> SignedManagedSessionLease:
        normalized = str(user_code or "").strip().upper()
        if (
            _USER_CODE.fullmatch(normalized) is None
            or _SAFE_ID.fullmatch(account_id) is None
        ):
            raise DeviceIdentityNotFound("device authorization was not found")
        with self._approval_lock:
            identity = self.account_directory.resolve(account_id)
            now = self._now().replace(microsecond=0)
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM device_identity_flows WHERE user_code_digest=?",
                    (self._commitment("user-code", normalized),),
                ).fetchone()
                if row is None:
                    raise DeviceIdentityNotFound("device authorization was not found")
                if row["status"] == "authorized":
                    if row["account_id"] != identity.account_id:
                        raise DeviceIdentityConflict(
                            "device authorization is already bound"
                        )
                    grant = connection.execute(
                        "SELECT lease_json FROM device_identity_grants WHERE flow_id=?",
                        (row["flow_id"],),
                    ).fetchone()
                    if grant is None:
                        raise DeviceIdentityConflict(
                            "device grant state is inconsistent"
                        )
                    return SignedManagedSessionLease.from_json(grant["lease_json"])
                if row["status"] != "pending" or _time(row["expires_at"]) <= now:
                    raise DeviceIdentityConflict("device authorization is not active")
                revision_row = connection.execute(
                    "SELECT high_water_revision FROM device_identity_account_revisions "
                    "WHERE account_id=?",
                    (identity.account_id,),
                ).fetchone()
                revision = int(revision_row[0]) + 1 if revision_row else 1
                flow_id = str(row["flow_id"])
                client_id = str(row["client_id"])
            finally:
                connection.close()

            access_jti = self._derived_id("access-jti", flow_id, revision)
            refresh_jti = self._derived_id("refresh-jti", flow_id, revision)
            access_expires_at = now + _TOKEN_TTL
            lease_expires_at = now + MAX_LEASE_DURATION
            access_token = self._access_token(
                identity,
                client_id=client_id,
                jti=access_jti,
                issued_at=now,
                expires_at=access_expires_at,
            )
            refresh_token = self._derived_token(
                "refresh-token", f"{flow_id}:{revision}", prefix="rft_"
            )
            claims = ManagedSessionLeaseClaims(
                lease_id="msl_" + self._derived_id("lease", flow_id, revision)[:40],
                account_id=identity.account_id,
                organization_id=identity.organization_id,
                display_name=identity.display_name,
                roles=identity.roles,
                model_allowlist=identity.model_allowlist,
                quota=identity.quota,
                admin_denies=identity.admin_denies,
                issued_at=now,
                expires_at=lease_expires_at,
                revision=revision,
                access_token_sha256=token_digest(access_token),
                refresh_token_sha256=token_digest(refresh_token),
            )
            lease = self._signed_lease(claims)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM device_identity_flows WHERE flow_id=?",
                    (flow_id,),
                ).fetchone()
                if current is None or current["status"] != "pending":
                    connection.rollback()
                    raise DeviceIdentityConflict("device authorization changed")
                high = connection.execute(
                    "SELECT high_water_revision FROM device_identity_account_revisions "
                    "WHERE account_id=?",
                    (identity.account_id,),
                ).fetchone()
                if (int(high[0]) if high else 0) >= revision:
                    connection.rollback()
                    raise DeviceIdentityConflict("device lease revision changed")
                connection.execute(
                    "INSERT INTO device_identity_account_revisions("
                    "account_id,high_water_revision,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET "
                    "high_water_revision=excluded.high_water_revision,"
                    "updated_at=excluded.updated_at",
                    (identity.account_id, revision, _iso(now)),
                )
                connection.execute(
                    "INSERT INTO device_identity_grants("
                    "flow_id,lease_id,lease_json,access_jti,refresh_jti,issued_at,"
                    "access_expires_at,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        flow_id,
                        claims.lease_id,
                        lease.to_json(),
                        access_jti,
                        refresh_jti,
                        _iso(now),
                        _iso(access_expires_at),
                        _iso(lease_expires_at),
                        _iso(now),
                    ),
                )
                connection.execute(
                    "INSERT INTO device_identity_grant_authority("
                    "lease_id,account_id,auth_epoch,source_lease_id,created_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        claims.lease_id,
                        identity.account_id,
                        identity.auth_epoch,
                        None,
                        _iso(now),
                    ),
                )
                connection.execute(
                    "UPDATE device_identity_flows SET status='authorized',account_id=?,"
                    "lease_revision=?,authorized_at=?,updated_at=? WHERE flow_id=?",
                    (
                        identity.account_id,
                        revision,
                        _iso(now),
                        _iso(now),
                        flow_id,
                    ),
                )
                self._audit(
                    connection,
                    "device.authorize.completed",
                    "authorized",
                    flow_id=flow_id,
                    account_id=identity.account_id,
                    details={"revision": revision},
                    now=now,
                )
                connection.commit()
                return lease
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def grant_account(
        self,
        *,
        client_id: str,
        account_id: str,
        idempotency_key: str,
    ) -> DeviceTokenResult:
        """Issue the normal signed device grant without exposing device codes."""

        self._client(client_id)
        self._idempotency(idempotency_key)
        identity = hashlib.sha256(
            b"ecorex-password-login-v1\0" + idempotency_key.encode("utf-8")
        ).hexdigest()
        challenge = self.begin(
            client_id=client_id,
            idempotency_key=f"password-login:{identity}",
        )
        self.approve(user_code=challenge.user_code, account_id=account_id)
        return self._grant(challenge.provider_flow_id, client_id=client_id)

    def refresh(
        self,
        *,
        client_id: str,
        lease_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> DeviceTokenResult:
        """Rotate a short access JWT while preserving the original policy TTL."""

        self._client(client_id)
        self._idempotency(idempotency_key)
        if (
            _SAFE_ID.fullmatch(str(lease_id or "")) is None
            or not isinstance(refresh_token, str)
            or not 16 <= len(refresh_token) <= 4096
        ):
            raise DeviceRefreshRequired("managed session refresh was rejected")
        request_hash = self._commitment(
            "refresh-request", f"{client_id}\0{lease_id}\0{idempotency_key}"
        )
        with self._approval_lock:
            connection = self._connect()
            try:
                source = self._source_grant(connection, lease_id)
                if source is None or source["client_id"] != client_id:
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                source_lease = SignedManagedSessionLease.from_json(source["lease_json"])
                if not hmac.compare_digest(
                    token_digest(refresh_token),
                    source_lease.claims.refresh_token_sha256,
                ):
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                authority = connection.execute(
                    "SELECT * FROM device_identity_grant_authority WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if (
                    authority is None
                    or authority["account_id"] != source_lease.claims.account_id
                    or self._lease_is_revoked(connection, lease_id)
                ):
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                try:
                    current_identity = self.account_directory.resolve(
                        source_lease.claims.account_id
                    )
                except DeviceIdentityError:
                    raise DeviceRefreshRequired(
                        "managed session refresh was rejected"
                    ) from None
                if int(authority["auth_epoch"]) != current_identity.auth_epoch:
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                replay = connection.execute(
                    "SELECT * FROM device_identity_refresh_grants "
                    "WHERE source_lease_id=?",
                    (lease_id,),
                ).fetchone()
                if replay is not None:
                    if replay["request_hash"] != request_hash:
                        raise DeviceIdentityConflict(
                            "managed session refresh identity changed"
                        )
                    if self._lease_is_revoked(connection, str(replay["lease_id"])):
                        raise DeviceRefreshRequired(
                            "managed session refresh was rejected"
                        )
                    return self._refresh_result(replay)
                now = self._now().replace(microsecond=0)
                if source_lease.claims.expires_at <= now + timedelta(seconds=30):
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                high = connection.execute(
                    "SELECT high_water_revision FROM device_identity_account_revisions "
                    "WHERE account_id=?",
                    (source_lease.claims.account_id,),
                ).fetchone()
                revision = max(
                    source_lease.claims.revision + 1,
                    (int(high[0]) + 1 if high else 1),
                )
            finally:
                connection.close()

            new_lease_id = (
                "msl_" + self._derived_id("refresh-lease", str(lease_id), revision)[:40]
            )
            access_jti = self._derived_id("refresh-access-jti", new_lease_id, revision)
            refresh_jti = self._derived_id("refresh-token-jti", new_lease_id, revision)
            access_expires_at = min(now + _TOKEN_TTL, source_lease.claims.expires_at)
            identity = DeviceAccountIdentity(
                account_id=source_lease.claims.account_id,
                organization_id=source_lease.claims.organization_id,
                display_name=source_lease.claims.display_name,
                roles=source_lease.claims.roles,
                model_allowlist=source_lease.claims.model_allowlist,
                quota=source_lease.claims.quota,
                admin_denies=source_lease.claims.admin_denies,
            )
            access_token = self._access_token(
                identity,
                client_id=client_id,
                jti=access_jti,
                issued_at=now,
                expires_at=access_expires_at,
            )
            new_refresh_token = self._derived_token(
                "refresh-token", f"refresh:{new_lease_id}:{revision}", prefix="rft_"
            )
            claims = ManagedSessionLeaseClaims(
                lease_id=new_lease_id,
                account_id=identity.account_id,
                organization_id=identity.organization_id,
                display_name=identity.display_name,
                roles=identity.roles,
                model_allowlist=identity.model_allowlist,
                quota=identity.quota,
                admin_denies=identity.admin_denies,
                issued_at=now,
                expires_at=source_lease.claims.expires_at,
                revision=revision,
                access_token_sha256=token_digest(access_token),
                refresh_token_sha256=token_digest(new_refresh_token),
            )
            lease = self._signed_lease(claims)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                concurrent = connection.execute(
                    "SELECT * FROM device_identity_refresh_grants "
                    "WHERE source_lease_id=?",
                    (lease_id,),
                ).fetchone()
                if concurrent is not None:
                    if concurrent["request_hash"] != request_hash:
                        raise DeviceIdentityConflict(
                            "managed session refresh identity changed"
                        )
                    connection.rollback()
                    return self._refresh_result(concurrent)
                high = connection.execute(
                    "SELECT high_water_revision FROM device_identity_account_revisions "
                    "WHERE account_id=?",
                    (identity.account_id,),
                ).fetchone()
                if (int(high[0]) if high else 0) >= revision:
                    raise DeviceIdentityConflict("managed session revision changed")
                connection.execute(
                    "INSERT INTO device_identity_account_revisions("
                    "account_id,high_water_revision,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET "
                    "high_water_revision=excluded.high_water_revision,"
                    "updated_at=excluded.updated_at",
                    (identity.account_id, revision, _iso(now)),
                )
                connection.execute(
                    "INSERT INTO device_identity_refresh_grants("
                    "source_lease_id,request_hash,client_id,account_id,lease_id,"
                    "lease_json,access_jti,refresh_jti,issued_at,access_expires_at,"
                    "lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        lease_id,
                        request_hash,
                        client_id,
                        identity.account_id,
                        new_lease_id,
                        lease.to_json(),
                        access_jti,
                        refresh_jti,
                        _iso(now),
                        _iso(access_expires_at),
                        _iso(claims.expires_at),
                        _iso(now),
                    ),
                )
                try:
                    latest_identity = self.account_directory.resolve(
                        identity.account_id
                    )
                except DeviceIdentityError:
                    raise DeviceRefreshRequired(
                        "managed session refresh was rejected"
                    ) from None
                if latest_identity.auth_epoch != current_identity.auth_epoch:
                    raise DeviceRefreshRequired("managed session refresh was rejected")
                connection.execute(
                    "INSERT INTO device_identity_grant_authority("
                    "lease_id,account_id,auth_epoch,source_lease_id,created_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        new_lease_id,
                        identity.account_id,
                        current_identity.auth_epoch,
                        lease_id,
                        _iso(now),
                    ),
                )
                self._audit(
                    connection,
                    "device.token.refreshed",
                    "authorized",
                    account_id=identity.account_id,
                    details={"revision": revision},
                    now=now,
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            return DeviceTokenResult(
                status="authorized",
                lease=lease,
                access_token=access_token,
                refresh_token=new_refresh_token,
            )

    def revoke(
        self,
        *,
        client_id: str,
        lease_id: str,
        account_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> DeviceRevocationResult:
        """Idempotently revoke one proven lease without requiring an active account."""

        self._client(client_id)
        self._idempotency(idempotency_key)
        if (
            _SAFE_ID.fullmatch(str(lease_id or "")) is None
            or _SAFE_ID.fullmatch(str(account_id or "")) is None
            or not isinstance(refresh_token, str)
            or not 16 <= len(refresh_token) <= 4096
        ):
            raise DeviceRefreshRequired("managed session revocation was rejected")
        idempotency_hash = self._commitment(
            "revoke-idempotency", f"{client_id}\0{idempotency_key}"
        )
        request_hash = self._commitment(
            "revoke-request", f"{client_id}\0{lease_id}\0{account_id}"
        )
        with self._approval_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                source = self._source_grant(connection, lease_id)
                if (
                    source is None
                    or source["client_id"] != client_id
                    or source["account_id"] != account_id
                ):
                    raise DeviceRefreshRequired(
                        "managed session revocation was rejected"
                    )
                lease = SignedManagedSessionLease.from_json(source["lease_json"])
                if not hmac.compare_digest(
                    token_digest(refresh_token),
                    lease.claims.refresh_token_sha256,
                ):
                    raise DeviceRefreshRequired(
                        "managed session revocation was rejected"
                    )
                replay = connection.execute(
                    "SELECT * FROM device_identity_revocations "
                    "WHERE idempotency_hash=?",
                    (idempotency_hash,),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["request_hash"] != request_hash
                        or replay["lease_id"] != lease_id
                        or replay["account_id"] != account_id
                        or replay["client_id"] != client_id
                    ):
                        raise DeviceIdentityConflict(
                            "managed session revoke identity changed"
                        )
                    connection.commit()
                    return DeviceRevocationResult(
                        lease_id=lease_id,
                        account_id=account_id,
                        already_revoked=True,
                    )
                existing = connection.execute(
                    "SELECT * FROM device_identity_revocations WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return DeviceRevocationResult(
                        lease_id=lease_id,
                        account_id=account_id,
                        already_revoked=True,
                    )
                now = self._now().replace(microsecond=0)
                connection.execute(
                    "INSERT INTO device_identity_revocations("
                    "lease_id,account_id,client_id,idempotency_hash,request_hash,"
                    "revoked_at) VALUES(?,?,?,?,?,?)",
                    (
                        lease_id,
                        account_id,
                        client_id,
                        idempotency_hash,
                        request_hash,
                        _iso(now),
                    ),
                )
                self._audit(
                    connection,
                    "device.session.revoked",
                    "revoked",
                    account_id=account_id,
                    details={"lease_hash": self._commitment("lease", lease_id)},
                    now=now,
                )
                connection.commit()
                return DeviceRevocationResult(
                    lease_id=lease_id,
                    account_id=account_id,
                    already_revoked=False,
                )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def verify_legacy_credential(self, *, user_code: str, credential: str) -> str:
        if not isinstance(credential, str) or not 8 <= len(credential) <= 4096:
            raise DeviceIdentityUnauthorized("legacy credential verification failed")
        normalized = str(user_code or "").strip().upper()
        if _USER_CODE.fullmatch(normalized) is None:
            raise DeviceIdentityUnauthorized("legacy credential verification failed")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            flow = connection.execute(
                "SELECT * FROM device_identity_flows WHERE user_code_digest=?",
                (self._commitment("user-code", normalized),),
            ).fetchone()
            if (
                flow is None
                or flow["status"] != "pending"
                or _time(flow["expires_at"]) <= now
                or int(flow["failed_verification_attempts"]) >= _MAX_LEGACY_ATTEMPTS
            ):
                connection.rollback()
                raise DeviceIdentityUnauthorized(
                    "legacy credential verification failed"
                )
            digest = self._legacy_digest_from_sha256(
                hashlib.sha256(credential.encode("utf-8")).hexdigest()
            )
            mapping = connection.execute(
                "SELECT account_id FROM device_identity_legacy_credentials "
                "WHERE credential_digest=? AND state='active'",
                (digest,),
            ).fetchone()
            if mapping is None:
                attempts = int(flow["failed_verification_attempts"]) + 1
                terminal = attempts >= _MAX_LEGACY_ATTEMPTS
                connection.execute(
                    "UPDATE device_identity_flows SET failed_verification_attempts=?,"
                    "status=?,updated_at=? WHERE flow_id=?",
                    (
                        attempts,
                        "denied" if terminal else "pending",
                        _iso(now),
                        flow["flow_id"],
                    ),
                )
                self._audit(
                    connection,
                    "device.legacy.verify",
                    "denied",
                    flow_id=str(flow["flow_id"]),
                    details={"terminal": terminal},
                    now=now,
                )
                connection.commit()
                raise DeviceIdentityUnauthorized(
                    "legacy credential verification failed"
                )
            account_id = str(mapping["account_id"])
            connection.commit()
        finally:
            connection.close()
        self.approve(user_code=normalized, account_id=account_id)
        return account_id

    def import_legacy_credentials(
        self, records: Iterable[Mapping[str, object]]
    ) -> dict[str, int]:
        imported = 0
        replayed = 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for raw in records:
                expected = {
                    "account_id",
                    "credential_sha256",
                    "display_name",
                    "email",
                    "role",
                    "daily_token_limit",
                    "weekly_token_limit",
                    "session_expires_at",
                    "source_record_sha256",
                }
                if set(raw) != expected:
                    raise ValueError("legacy credential record contract is invalid")
                account_id = str(raw["account_id"])
                credential_sha256 = str(raw["credential_sha256"]).casefold()
                source_hash = str(raw["source_record_sha256"]).casefold()
                if (
                    _SAFE_ID.fullmatch(account_id) is None
                    or _SOURCE_HASH.fullmatch(credential_sha256) is None
                    or _SOURCE_HASH.fullmatch(source_hash) is None
                    or not isinstance(raw["display_name"], str)
                    or not 1 <= len(raw["display_name"]) <= 256
                    or not isinstance(raw["email"], str)
                    or not 3 <= len(raw["email"]) <= 254
                    or not isinstance(raw["role"], str)
                    or not 1 <= len(raw["role"]) <= 64
                    or isinstance(raw["daily_token_limit"], bool)
                    or not isinstance(raw["daily_token_limit"], int)
                    or isinstance(raw["weekly_token_limit"], bool)
                    or not isinstance(raw["weekly_token_limit"], int)
                    or not isinstance(raw["session_expires_at"], str)
                ):
                    raise ValueError("legacy credential record is invalid")
                _time(str(raw["session_expires_at"]))
                # Refuse orphaned mappings before any credential commitment is stored.
                self.account_directory.resolve(account_id)
                digest = self._legacy_digest_from_sha256(credential_sha256)
                existing = connection.execute(
                    "SELECT credential_digest,account_id,source_record_hash FROM "
                    "device_identity_legacy_credentials WHERE credential_digest=? "
                    "OR source_record_hash=?",
                    (digest, source_hash),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or tuple(existing[0]) != (
                        digest,
                        account_id,
                        source_hash,
                    ):
                        raise DeviceIdentityConflict(
                            "legacy credential mapping identity changed"
                        )
                    replayed += 1
                    continue
                connection.execute(
                    "INSERT INTO device_identity_legacy_credentials("
                    "credential_digest,account_id,source_version,state,imported_at,"
                    "source_record_hash) VALUES(?,?,'0.2.9.2','active',?,?)",
                    (digest, account_id, _iso(self._now()), source_hash),
                )
                self._audit(
                    connection,
                    "device.legacy.imported",
                    "imported",
                    account_id=account_id,
                    details={"source_version": "0.2.9.2"},
                    now=self._now(),
                )
                imported += 1
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return {"imported": imported, "replayed": replayed}

    def _grant(self, flow_id: str, *, client_id: str) -> DeviceTokenResult:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT flows.client_id,flows.account_id,grants.* "
                "FROM device_identity_flows flows JOIN device_identity_grants grants "
                "USING(flow_id) WHERE flows.flow_id=? AND flows.status='authorized'",
                (flow_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["client_id"] != client_id:
            raise DeviceIdentityConflict("device grant is unavailable")
        if _time(row["access_expires_at"]) <= self._now():
            return DeviceTokenResult(status="expired", retry_after_seconds=None)
        lease = SignedManagedSessionLease.from_json(row["lease_json"])
        # Recheck that the account still exists/is active, while keeping the
        # exact approved entitlement snapshot bound to the signed lease.
        current_identity = self.account_directory.resolve(str(row["account_id"]))
        connection = self._connect()
        try:
            authority = connection.execute(
                "SELECT auth_epoch FROM device_identity_grant_authority "
                "WHERE lease_id=?",
                (lease.claims.lease_id,),
            ).fetchone()
            revoked = self._lease_is_revoked(connection, lease.claims.lease_id)
        finally:
            connection.close()
        if (
            authority is None
            or int(authority["auth_epoch"]) != current_identity.auth_epoch
            or revoked
        ):
            raise DeviceRefreshRequired("managed session grant was rejected")
        identity = DeviceAccountIdentity(
            account_id=lease.claims.account_id,
            organization_id=lease.claims.organization_id,
            display_name=lease.claims.display_name,
            roles=lease.claims.roles,
            model_allowlist=lease.claims.model_allowlist,
            quota=lease.claims.quota,
            admin_denies=lease.claims.admin_denies,
        )
        issued_at = _time(row["issued_at"])
        access_token = self._access_token(
            identity,
            client_id=client_id,
            jti=str(row["access_jti"]),
            issued_at=issued_at,
            expires_at=_time(row["access_expires_at"]),
        )
        refresh_token = self._derived_token(
            "refresh-token",
            f"{flow_id}:{lease.claims.revision}",
            prefix="rft_",
        )
        if (
            token_digest(access_token) != lease.claims.access_token_sha256
            or token_digest(refresh_token) != lease.claims.refresh_token_sha256
        ):
            raise DeviceIdentityConflict("device grant commitment is inconsistent")
        return DeviceTokenResult(
            status="authorized",
            lease=lease,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    @staticmethod
    def _source_grant(
        connection: sqlite3.Connection, lease_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT flows.client_id,flows.account_id,grants.lease_json,"
            "grants.issued_at,grants.access_expires_at,grants.lease_expires_at "
            "FROM device_identity_grants grants "
            "JOIN device_identity_flows flows USING(flow_id) "
            "WHERE grants.lease_id=? "
            "UNION ALL "
            "SELECT client_id,account_id,lease_json,issued_at,access_expires_at,"
            "lease_expires_at FROM device_identity_refresh_grants WHERE lease_id=? "
            "LIMIT 1",
            (lease_id, lease_id),
        ).fetchone()

    @staticmethod
    def _lease_is_revoked(
        connection: sqlite3.Connection,
        lease_id: str,
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM device_identity_revocations WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            is not None
        )

    def _refresh_result(self, row: sqlite3.Row) -> DeviceTokenResult:
        lease = SignedManagedSessionLease.from_json(row["lease_json"])
        identity = DeviceAccountIdentity(
            account_id=lease.claims.account_id,
            organization_id=lease.claims.organization_id,
            display_name=lease.claims.display_name,
            roles=lease.claims.roles,
            model_allowlist=lease.claims.model_allowlist,
            quota=lease.claims.quota,
            admin_denies=lease.claims.admin_denies,
        )
        access_token = self._access_token(
            identity,
            client_id=str(row["client_id"]),
            jti=str(row["access_jti"]),
            issued_at=_time(row["issued_at"]),
            expires_at=_time(row["access_expires_at"]),
        )
        refresh_token = self._derived_token(
            "refresh-token",
            f"refresh:{lease.claims.lease_id}:{lease.claims.revision}",
            prefix="rft_",
        )
        if (
            token_digest(access_token) != lease.claims.access_token_sha256
            or token_digest(refresh_token) != lease.claims.refresh_token_sha256
        ):
            raise DeviceIdentityConflict("managed session refresh is inconsistent")
        return DeviceTokenResult(
            status="authorized",
            lease=lease,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def _challenge(self, row: sqlite3.Row) -> DeviceChallenge:
        flow_id = str(row["flow_id"])
        return DeviceChallenge(
            provider_flow_id=flow_id,
            device_code=self._derived_token("device-code", flow_id, prefix="dc_"),
            user_code=self._user_code(flow_id),
            verification_url=self.verification_url,
            expires_at=_time(row["expires_at"]),
        )

    def _signed_lease(
        self, claims: ManagedSessionLeaseClaims
    ) -> SignedManagedSessionLease:
        try:
            signature = self.lease_signer.sign(claims.canonical_payload())
        except Exception as error:
            raise DeviceIdentityUnavailable(
                f"managed-session signer failed safely: {type(error).__name__}"
            ) from None
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise DeviceIdentityUnavailable(
                "managed-session signer response is invalid"
            )
        return SignedManagedSessionLease(
            claims=claims,
            signature=SessionLeaseSignature(
                algorithm="ed25519",
                key_id=self.lease_signer.key_id,
                value=base64.b64encode(signature).decode("ascii"),
            ),
        )

    def _access_token(
        self,
        identity: DeviceAccountIdentity,
        *,
        client_id: str,
        jti: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> str:
        header = {"alg": "EdDSA", "kid": self.access_signer.key_id, "typ": "JWT"}
        request_limit = max(1, int(identity.quota.get("managed_requests", 1)))
        concurrent_limit = max(
            1, min(1000, int(identity.quota.get("concurrent_requests", 1)))
        )
        claims: dict[str, object] = {
            "iss": self.issuer,
            "aud": self.audience,
            "token_use": "access",
            "sub": identity.account_id,
            "client_id": client_id,
            "account_id": identity.account_id,
            "organization_id": identity.organization_id,
            "roles": list(identity.roles),
            "allowed_model_ids": list(identity.model_allowlist),
            "quota_period": "managed-session",
            "request_limit": request_limit,
            "concurrent_request_limit": concurrent_limit,
            "jti": jti,
            "iat": int(issued_at.timestamp()),
            "nbf": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        signing_input = _b64url(_canonical(header)) + "." + _b64url(_canonical(claims))
        try:
            signature = self.access_signer.sign(signing_input.encode("ascii"))
        except Exception as error:
            raise DeviceIdentityUnavailable(
                f"access-token signer failed safely: {type(error).__name__}"
            ) from None
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise DeviceIdentityUnavailable("access-token signer response is invalid")
        return signing_input + "." + _b64url(signature)

    def _derived_token(self, kind: str, identity: str, *, prefix: str) -> str:
        material = hmac.new(
            self.secrets.derivation_key,
            f"ecorex-device-identity-v1\0{kind}\0{identity}".encode(),
            hashlib.sha256,
        ).digest()
        return prefix + _b64url(material)

    def _derived_id(self, kind: str, flow_id: str, revision: int) -> str:
        return hmac.new(
            self.secrets.derivation_key,
            f"ecorex-device-identity-v1\0{kind}\0{flow_id}\0{revision}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _user_code(self, flow_id: str) -> str:
        raw = hmac.new(
            self.secrets.derivation_key,
            f"ecorex-device-user-code-v1\0{flow_id}".encode(),
            hashlib.sha256,
        ).digest()[:5]
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        number = int.from_bytes(raw, "big")
        chars = "".join(alphabet[(number >> shift) & 31] for shift in range(35, -1, -5))
        return chars[:4] + "-" + chars[4:8]

    def _commitment(self, kind: str, value: str) -> str:
        return hmac.new(
            self.secrets.derivation_key,
            f"ecorex-device-commitment-v1\0{kind}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _legacy_digest_from_sha256(self, credential_sha256: str) -> str:
        if _SOURCE_HASH.fullmatch(credential_sha256) is None:
            raise ValueError("legacy credential digest is invalid")
        return hmac.new(
            self.secrets.legacy_credential_pepper,
            b"ecorex-v0.2.9.2-credential-v1\0" + credential_sha256.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _audit(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        outcome: str,
        *,
        flow_id: str | None = None,
        account_id: str | None = None,
        details: Mapping[str, object],
        now: datetime,
    ) -> None:
        previous = connection.execute(
            "SELECT entry_digest FROM device_identity_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_digest = str(previous[0]) if previous else None
        details_json = _canonical(dict(details)).decode("utf-8")
        flow_hash = self._commitment("audit-flow", flow_id) if flow_id else None
        account_hash = (
            self._commitment("audit-account", account_id) if account_id else None
        )
        document = _canonical(
            {
                "event_type": event_type,
                "outcome": outcome,
                "flow_hash": flow_hash,
                "account_hash": account_hash,
                "details": json.loads(details_json),
                "previous_digest": previous_digest,
                "created_at": _iso(now),
            }
        )
        entry_digest = hashlib.sha256(
            b"ecorex-device-identity-audit-v1\n" + document
        ).hexdigest()
        connection.execute(
            "INSERT INTO device_identity_audit("
            "event_type,outcome,flow_hash,account_hash,details_json,previous_digest,"
            "entry_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                event_type,
                outcome,
                flow_hash,
                account_hash,
                details_json,
                previous_digest,
                entry_digest,
                _iso(now),
            ),
        )

    def _client(self, client_id: str) -> None:
        if client_id not in self.allowed_client_ids:
            raise DeviceIdentityUnauthorized("device client is not authorized")

    @staticmethod
    def _idempotency(value: str) -> None:
        if not isinstance(value, str) or _IDEMPOTENCY.fullmatch(value) is None:
            raise DeviceIdentityUnauthorized("device request identity is invalid")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise DeviceIdentityUnavailable("device identity clock is invalid")
        return value.astimezone(UTC)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise DeviceIdentityConflict("device identity timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise DeviceIdentityConflict("device identity timestamp is invalid")
    return parsed.astimezone(UTC)


def _bounded_text(value: str, label: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _https_url(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(normalized) > 2048
    ):
        raise ValueError("device verification URL is invalid")
    return normalized


__all__ = [
    "DeviceAccountDirectory",
    "DeviceAccountIdentity",
    "DeviceChallenge",
    "DeviceIdentityConflict",
    "DeviceIdentityError",
    "DeviceIdentityNotFound",
    "DeviceRefreshRequired",
    "DeviceIdentitySecrets",
    "DeviceIdentityUnauthorized",
    "DeviceIdentityUnavailable",
    "DeviceTokenResult",
    "DeviceRevocationResult",
    "ManagedDeviceIdentityBroker",
]

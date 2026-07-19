"""Transactional product administration for users, usage and managed models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Mapping
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .management_models import (
    ActiveModelConfiguration,
    AdjustUsageRequest,
    AdminUserListProjection,
    AdminUserProjection,
    CreateAdminUserRequest,
    CreateModelConfigurationRequest,
    ModelConfigurationProjection,
    ModelRevisionProjection,
    ModelTestProjection,
    StageModelConfigurationRequest,
    UpdateAdminUserRequest,
    UsageSummaryProjection,
    provider_origin_preset_for_slot,
    provider_protocol_for_slot,
)
from .management_schema import AdminManagementSchemaManager
from .models import ControlPrincipal
from .model_activation import (
    HTTPSModelConnectionTester,
    ModelConnectionTester,
    ModelConnectionTestResult,
    RejectingModelConnectionTester,
)
from .password_credentials import (
    dummy_password_hash,
    encode_password,
    verify_password_and_upgrade,
)


_ZERO_DIGEST = "0" * 64
_PASSWORD_RATE_WINDOW = timedelta(minutes=15)
_PASSWORD_RATE_TTL = timedelta(hours=1)
_PASSWORD_ACCOUNT_LIMIT = 5
_PASSWORD_IP_LIMIT = 20
_PASSWORD_RATE_CAPACITY = 100_000


class AdminManagementError(RuntimeError):
    pass


class AdminManagementConflict(AdminManagementError):
    pass


class AdminManagementNotFound(AdminManagementError):
    pass


class AdminPasswordAuthenticationError(AdminManagementError):
    pass


class AdminPasswordLocked(AdminPasswordAuthenticationError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("password login is temporarily locked")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class AdminModelSecretError(AdminManagementError):
    pass


@dataclass(frozen=True, slots=True)
class ModelTestLease:
    test_id: str
    configuration: ActiveModelConfiguration


class _ModelSecretCipher:
    _DOMAIN = b"ecorex-admin-model-secret-v1\0"
    _PASSWORD_FINGERPRINT_DOMAIN = (
        b"ecorex-admin-password-idempotency-fingerprint-v1\0"
    )

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("managed model encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)
        self._password_fingerprint_key = hmac.new(
            key,
            self._PASSWORD_FINGERPRINT_DOMAIN,
            hashlib.sha256,
        ).digest()

    @staticmethod
    def fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def encrypt(self, secret_id: str, value: str) -> tuple[bytes, bytes, str]:
        if (
            not isinstance(value, str)
            or not 8 <= len(value) <= 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ValueError("managed model key is invalid")
        nonce = secrets.token_bytes(12)
        associated = self._DOMAIN + secret_id.encode("ascii")
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), associated)
        return nonce, ciphertext, self.fingerprint(value)

    def decrypt(self, secret_id: str, nonce: bytes, ciphertext: bytes) -> str:
        try:
            plaintext = self._cipher.decrypt(
                bytes(nonce),
                bytes(ciphertext),
                self._DOMAIN + secret_id.encode("ascii"),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError):
            raise AdminModelSecretError("managed model key cannot be decrypted") from None

    def password_request_fingerprint(self, value: str) -> str:
        return hmac.new(
            self._password_fingerprint_key,
            self._PASSWORD_FINGERPRINT_DOMAIN + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


_PROVIDER_USAGE_SERVICES = frozenset({"managed_gateway", "image_service"})
_PROVIDER_USAGE_KINDS = frozenset({"chat", "image"})
_PROVIDER_USAGE_ACTOR = ControlPrincipal(
    subject="system:provider-usage-settlement",
    client_id="ecorex-internal",
    account_id="system",
    roles=frozenset({"platform_admin"}),
)
_PASSWORD_AUTH_ACTOR = ControlPrincipal(
    subject="system:password-authentication",
    client_id="ecorex-control-plane",
    account_id="system",
    roles=frozenset({"platform_admin"}),
)


class AdminManagementRepository:
    """One SQLite authority with immutable model revisions and secret-safe APIs."""

    def __init__(self, path: str | Path, *, encryption_key: bytes) -> None:
        self.path = Path(path).expanduser().resolve()
        AdminManagementSchemaManager(self.path).validate()
        self._secrets = _ModelSecretCipher(encryption_key)
        self.verify_integrity()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def list_users(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        organization_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> AdminUserListProjection:
        if status not in {None, "active", "suspended"}:
            raise ValueError("user status filter is invalid")
        if not 0 <= offset <= 10**9 or not 1 <= limit <= 200:
            raise ValueError("user pagination is invalid")
        filters: list[str] = []
        values: list[object] = []
        if query:
            normalized = query.strip()
            if len(normalized) > 128:
                raise ValueError("user search is too long")
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(
                "(account_id LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\' "
                "OR email LIKE ? ESCAPE '\\')"
            )
            values.extend([f"%{escaped}%"] * 3)
        if status is not None:
            filters.append("status=?")
            values.append(status)
        if organization_id is not None:
            filters.append("organization_id=?")
            values.append(organization_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM admin_ops_users" + where,
                    values,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM admin_ops_users"
                + where
                + " ORDER BY updated_at DESC,account_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            return AdminUserListProjection(
                items=[self._user(connection, row) for row in rows],
                total=total,
                offset=offset,
                limit=limit,
            )
        finally:
            connection.close()

    def get_user(self, account_id: str) -> AdminUserProjection:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?", (account_id,)
            ).fetchone()
            if row is None:
                raise AdminManagementNotFound("user does not exist")
            return self._user(connection, row)
        finally:
            connection.close()

    def create_user(
        self, request: CreateAdminUserRequest, *, actor: ControlPrincipal
    ) -> AdminUserProjection:
        password_hash = password_fingerprint = None
        if request.password is not None:
            password = request.password.get_secret_value()
            try:
                password_hash = encode_password(password)
                password_fingerprint = self._secrets.password_request_fingerprint(
                    password
                )
            finally:
                password = ""
        idempotency_request = request.model_dump(
            mode="json", exclude={"password"}
        ) | {
            "password_set": password_hash is not None,
            "password_input_fingerprint": password_fingerprint,
        }
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="user.create",
            request_payload=idempotency_request,
            projection=AdminUserProjection,
            action=lambda connection: self._create_user(
                connection, request, actor, password_hash=password_hash
            ),
        )

    def _create_user(
        self,
        connection: sqlite3.Connection,
        request: CreateAdminUserRequest,
        actor: ControlPrincipal,
        *,
        password_hash: str | None,
    ) -> AdminUserProjection:
        now = _now()
        self._require_available_identity_namespace(
            connection,
            account_id=request.account_id,
            email=request.email,
        )
        try:
            connection.execute(
                "INSERT INTO admin_ops_users("
                "account_id,display_name,email,organization_id,status,token_limit,"
                "tokens_used,image_limit,images_used,revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.account_id,
                    request.display_name,
                    request.email,
                    request.organization_id,
                    "active",
                    request.token_limit,
                    0,
                    request.image_limit,
                    0,
                    1,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise AdminManagementConflict("user identity already exists") from None
        if password_hash is not None:
            self._write_password(
                connection,
                account_id=request.account_id,
                encoded_hash=password_hash,
                source_version="admin",
                source_record_sha256=None,
                now=now,
            )
        self._audit(
            connection,
            actor=actor,
            action="user.create",
            target_id=request.account_id,
            payload={
                **request.model_dump(
                    mode="json",
                    exclude={"client_request_id", "password"},
                ),
                "password_set": password_hash is not None,
            },
        )
        return self._user(
            connection,
            connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?",
                (request.account_id,),
            ).fetchone()
        )

    def update_user(
        self,
        account_id: str,
        request: UpdateAdminUserRequest,
        *,
        actor: ControlPrincipal,
    ) -> AdminUserProjection:
        password_hash = password_fingerprint = None
        if request.password is not None:
            password = request.password.get_secret_value()
            try:
                password_hash = encode_password(password)
                password_fingerprint = self._secrets.password_request_fingerprint(
                    password
                )
            finally:
                password = ""
        payload = request.model_dump(mode="json", exclude={"password"}) | {
            "account_id": account_id,
            "password_set": password_hash is not None,
            "password_input_fingerprint": password_fingerprint,
        }
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="user.update",
            request_payload=payload,
            projection=AdminUserProjection,
            action=lambda connection: self._update_user(
                connection,
                account_id,
                request,
                actor,
                password_hash=password_hash,
            ),
        )

    def _update_user(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        request: UpdateAdminUserRequest,
        actor: ControlPrincipal,
        *,
        password_hash: str | None,
    ) -> AdminUserProjection:
        now = _now()
        self._require_available_identity_namespace(
            connection,
            account_id=account_id,
            email=request.email,
            exclude_account_id=account_id,
        )
        try:
            cursor = connection.execute(
                "UPDATE admin_ops_users SET display_name=?,email=?,organization_id=?,"
                "status=?,token_limit=?,image_limit=?,revision=revision+1,updated_at=? "
                "WHERE account_id=? AND revision=?",
                (
                    request.display_name,
                    request.email,
                    request.organization_id,
                    request.status,
                    request.token_limit,
                    request.image_limit,
                    now,
                    account_id,
                    request.expected_revision,
                ),
            )
        except sqlite3.IntegrityError:
            raise AdminManagementConflict("user email already exists") from None
        if cursor.rowcount != 1:
            self._require_user_or_conflict(connection, account_id)
        if password_hash is not None:
            self._write_password(
                connection,
                account_id=account_id,
                encoded_hash=password_hash,
                source_version="admin",
                source_record_sha256=None,
                now=now,
            )
        self._audit(
            connection,
            actor=actor,
            action="user.update",
            target_id=account_id,
            payload={
                **request.model_dump(
                    mode="json",
                    exclude={"client_request_id", "password"},
                ),
                "password_set": password_hash is not None,
            },
        )
        return self._user(
            connection,
            connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?", (account_id,)
            ).fetchone()
        )

    def authenticate_password(
        self,
        identifier: str,
        password: str,
        *,
        source_ip: str | None = None,
        now: datetime | None = None,
    ) -> AdminUserProjection:
        normalized = str(identifier or "").strip()
        if (
            not 1 <= len(normalized) <= 254
            or "\x00" in normalized
            or any(ord(character) < 33 for character in normalized)
        ):
            raise AdminPasswordAuthenticationError("password login failed")
        email = normalized.casefold()
        identifier_sha = hashlib.sha256(
            b"ecorex-password-identifier-v1\0" + email.encode("utf-8")
        ).hexdigest()
        source_key = self._password_source_key(source_ip)
        selected_clock = now or datetime.now(UTC)
        if selected_clock.tzinfo is None:
            raise ValueError("password authentication clock must be timezone-aware")
        selected_now = selected_clock.astimezone(UTC)

        reservations = (
            ("account", identifier_sha, _PASSWORD_ACCOUNT_LIMIT),
            ("ip", source_key, _PASSWORD_IP_LIMIT),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_password_rate_limits(connection, selected_now)
            retry_after = self._reserve_password_attempts(
                connection,
                reservations=reservations,
                now=selected_now,
            )
            if retry_after is not None:
                self._audit_password_authentication(
                    connection,
                    identifier_sha=identifier_sha,
                    source_sha=source_key,
                    outcome="limited",
                )
                connection.commit()
                raise AdminPasswordLocked(retry_after)
            rows = connection.execute(
                "SELECT users.*,credentials.encoded_hash "
                "FROM admin_ops_users users LEFT JOIN admin_ops_password_credentials "
                "credentials USING(account_id) WHERE users.account_id=? OR users.email=? "
                "ORDER BY users.account_id",
                (normalized, email),
            ).fetchall()
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        candidate = rows[0] if len(rows) == 1 else None
        encoded = (
            str(candidate["encoded_hash"])
            if candidate is not None and candidate["encoded_hash"] is not None
            else dummy_password_hash()
        )
        verified, replacement_hash = verify_password_and_upgrade(password, encoded)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = None
            if candidate is not None:
                current = connection.execute(
                    "SELECT users.*,credentials.encoded_hash "
                    "FROM admin_ops_users users LEFT JOIN "
                    "admin_ops_password_credentials credentials USING(account_id) "
                    "WHERE users.account_id=?",
                    (candidate["account_id"],),
                ).fetchone()
            success = bool(
                verified
                and current is not None
                and current["status"] == "active"
                and current["encoded_hash"] is not None
                and str(current["encoded_hash"]) == encoded
            )
            if success:
                self._release_password_reservations(
                    connection,
                    reservations=reservations,
                    now=selected_now,
                )
                if replacement_hash is not None:
                    self._write_password(
                        connection,
                        account_id=str(current["account_id"]),
                        encoded_hash=replacement_hash,
                        source_version="admin",
                        source_record_sha256=None,
                        now=selected_now.isoformat(),
                    )
                self._audit_password_authentication(
                    connection,
                    identifier_sha=identifier_sha,
                    source_sha=source_key,
                    outcome="succeeded",
                )
                connection.commit()
                assert current is not None
                return self._user(connection, current)
            retry_after = self._finalize_password_failure(
                connection,
                reservations=reservations,
                now=selected_now,
            )
            self._audit_password_authentication(
                connection,
                identifier_sha=identifier_sha,
                source_sha=source_key,
                outcome="failed",
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        if retry_after is not None:
            raise AdminPasswordLocked(retry_after)
        raise AdminPasswordAuthenticationError("password login failed")

    @staticmethod
    def _password_source_key(source_ip: str | None) -> str:
        if source_ip is None:
            normalized = "unattributed"
        else:
            try:
                normalized = ipaddress.ip_address(source_ip.strip()).compressed
            except (AttributeError, ValueError):
                raise ValueError("password authentication source is invalid") from None
        return hashlib.sha256(
            b"ecorex-password-source-v1\0" + normalized.encode("ascii")
        ).hexdigest()

    @staticmethod
    def _cleanup_password_rate_limits(
        connection: sqlite3.Connection,
        now: datetime,
    ) -> None:
        connection.execute(
            "DELETE FROM admin_ops_password_failures WHERE updated_at < ?",
            ((now - _PASSWORD_RATE_TTL).isoformat(),),
        )

    @classmethod
    def _reserve_password_attempts(
        cls,
        connection: sqlite3.Connection,
        *,
        reservations: tuple[tuple[str, str, int], ...],
        now: datetime,
    ) -> int | None:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM admin_ops_password_failures"
            ).fetchone()[0]
        )
        missing = sum(
            connection.execute(
                "SELECT 1 FROM admin_ops_password_failures "
                "WHERE scope=? AND subject_sha256=?",
                (scope, subject),
            ).fetchone()
            is None
            for scope, subject, _limit in reservations
        )
        if count + missing > _PASSWORD_RATE_CAPACITY:
            return 60
        retry_after = 0
        prepared: list[tuple[str, str, int, datetime]] = []
        for scope, subject, limit in reservations:
            row = connection.execute(
                "SELECT * FROM admin_ops_password_failures "
                "WHERE scope=? AND subject_sha256=?",
                (scope, subject),
            ).fetchone()
            attempts, window_started, locked_until = cls._password_rate_state(
                row, now
            )
            if locked_until is not None and locked_until > now:
                retry_after = max(
                    retry_after,
                    max(1, int((locked_until - now).total_seconds())),
                )
                continue
            if attempts >= limit:
                until = max(window_started + _PASSWORD_RATE_WINDOW, now)
                retry_after = max(
                    retry_after,
                    max(1, int((until - now).total_seconds())),
                )
                continue
            prepared.append((scope, subject, attempts + 1, window_started))
        if retry_after:
            return retry_after
        for scope, subject, attempts, window_started in prepared:
            connection.execute(
                "INSERT INTO admin_ops_password_failures("
                "scope,subject_sha256,failed_attempts,window_started_at,"
                "locked_until,updated_at) VALUES(?,?,?,?,NULL,?) "
                "ON CONFLICT(scope,subject_sha256) DO UPDATE SET "
                "failed_attempts=excluded.failed_attempts,"
                "window_started_at=excluded.window_started_at,"
                "locked_until=NULL,updated_at=excluded.updated_at",
                (
                    scope,
                    subject,
                    attempts,
                    window_started.isoformat(),
                    now.isoformat(),
                ),
            )
        return None

    @staticmethod
    def _password_rate_state(
        row: sqlite3.Row | None,
        now: datetime,
    ) -> tuple[int, datetime, datetime | None]:
        if row is None:
            return 0, now, None
        window_started = datetime.fromisoformat(str(row["window_started_at"]))
        if window_started.tzinfo is None:
            raise AdminManagementError("password failure state is invalid")
        window_started = window_started.astimezone(UTC)
        if now - window_started >= _PASSWORD_RATE_WINDOW:
            return 0, now, None
        locked_until = None
        if row["locked_until"] is not None:
            locked_until = datetime.fromisoformat(str(row["locked_until"]))
            if locked_until.tzinfo is None:
                raise AdminManagementError("password lock state is invalid")
            locked_until = locked_until.astimezone(UTC)
        return int(row["failed_attempts"]), window_started, locked_until

    @classmethod
    def _finalize_password_failure(
        cls,
        connection: sqlite3.Connection,
        *,
        reservations: tuple[tuple[str, str, int], ...],
        now: datetime,
    ) -> int | None:
        retry_after = 0
        for scope, subject, limit in reservations:
            row = connection.execute(
                "SELECT * FROM admin_ops_password_failures "
                "WHERE scope=? AND subject_sha256=?",
                (scope, subject),
            ).fetchone()
            attempts, _started, _locked = cls._password_rate_state(row, now)
            if attempts < limit:
                continue
            locked_until = now + _PASSWORD_RATE_WINDOW
            connection.execute(
                "UPDATE admin_ops_password_failures SET locked_until=?,updated_at=? "
                "WHERE scope=? AND subject_sha256=?",
                (
                    locked_until.isoformat(),
                    now.isoformat(),
                    scope,
                    subject,
                ),
            )
            retry_after = max(retry_after, int(_PASSWORD_RATE_WINDOW.total_seconds()))
        return retry_after or None

    @staticmethod
    def _release_password_reservations(
        connection: sqlite3.Connection,
        *,
        reservations: tuple[tuple[str, str, int], ...],
        now: datetime,
    ) -> None:
        for scope, subject, _limit in reservations:
            row = connection.execute(
                "SELECT failed_attempts FROM admin_ops_password_failures "
                "WHERE scope=? AND subject_sha256=?",
                (scope, subject),
            ).fetchone()
            if row is None:
                continue
            attempts = int(row["failed_attempts"])
            if attempts <= 1:
                connection.execute(
                    "DELETE FROM admin_ops_password_failures "
                    "WHERE scope=? AND subject_sha256=?",
                    (scope, subject),
                )
            else:
                connection.execute(
                    "UPDATE admin_ops_password_failures SET "
                    "failed_attempts=failed_attempts-1,locked_until=NULL,updated_at=? "
                    "WHERE scope=? AND subject_sha256=?",
                    (now.isoformat(), scope, subject),
                )

    def _audit_password_authentication(
        self,
        connection: sqlite3.Connection,
        *,
        identifier_sha: str,
        source_sha: str,
        outcome: str,
    ) -> None:
        self._audit(
            connection,
            actor=_PASSWORD_AUTH_ACTOR,
            action=f"password.login.{outcome}",
            target_id=identifier_sha,
            payload={
                "identifier_sha256": identifier_sha,
                "source_sha256": source_sha,
                "outcome": outcome,
            },
        )

    @staticmethod
    def _write_password(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        encoded_hash: str,
        source_version: str,
        source_record_sha256: str | None,
        now: str,
    ) -> None:
        existing = connection.execute(
            "SELECT credential_version FROM admin_ops_password_credentials "
            "WHERE account_id=?",
            (account_id,),
        ).fetchone()
        version = int(existing[0]) + 1 if existing is not None else 1
        connection.execute(
            "INSERT INTO admin_ops_password_credentials("
            "account_id,algorithm,encoded_hash,credential_version,source_version,"
            "source_record_sha256,password_changed_at,updated_at"
            ") VALUES(?,'pbkdf2_sha256',?,?,?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET algorithm=excluded.algorithm,"
            "encoded_hash=excluded.encoded_hash,"
            "credential_version=excluded.credential_version,"
            "source_version=excluded.source_version,source_record_sha256=NULL,"
            "password_changed_at=excluded.password_changed_at,"
            "updated_at=excluded.updated_at",
            (
                account_id,
                encoded_hash,
                version,
                source_version,
                source_record_sha256,
                now,
                now,
            ),
        )

    def adjust_usage(
        self,
        account_id: str,
        request: AdjustUsageRequest,
        *,
        actor: ControlPrincipal,
    ) -> AdminUserProjection:
        payload = request.model_dump(mode="json") | {"account_id": account_id}
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="usage.adjust",
            request_payload=payload,
            projection=AdminUserProjection,
            action=lambda connection: self._adjust_usage(
                connection, account_id, request, actor
            ),
        )

    def _adjust_usage(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        request: AdjustUsageRequest,
        actor: ControlPrincipal,
    ) -> AdminUserProjection:
        row = connection.execute(
            "SELECT * FROM admin_ops_users WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise AdminManagementNotFound("user does not exist")
        if int(row["revision"]) != request.expected_revision:
            raise AdminManagementConflict("user revision changed")
        tokens = int(row["tokens_used"]) + request.token_delta
        images = int(row["images_used"]) + request.image_delta
        if tokens < 0 or images < 0:
            raise AdminManagementConflict("usage adjustment would become negative")
        now = _now()
        revision = int(row["revision"]) + 1
        cursor = connection.execute(
            "UPDATE admin_ops_users SET tokens_used=?,images_used=?,revision=?,updated_at=? "
            "WHERE account_id=? AND revision=?",
            (tokens, images, revision, now, account_id, request.expected_revision),
        )
        if cursor.rowcount != 1:
            raise AdminManagementConflict("user revision changed")
        adjustment_id = "usage_" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO admin_ops_usage_ledger("
            "adjustment_id,account_id,token_delta,image_delta,reason,actor_subject,"
            "resulting_user_revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                adjustment_id,
                account_id,
                request.token_delta,
                request.image_delta,
                request.reason,
                actor.subject,
                revision,
                now,
            ),
        )
        self._audit(
            connection,
            actor=actor,
            action="usage.adjust",
            target_id=account_id,
            payload={
                "adjustment_id": adjustment_id,
                "token_delta": request.token_delta,
                "image_delta": request.image_delta,
                "reason": request.reason,
                "resulting_user_revision": revision,
            },
        )
        return self._user(
            connection,
            connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?", (account_id,)
            ).fetchone()
        )

    def usage_summary(self) -> UsageSummaryProjection:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS users_total,"
                "COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END),0) AS users_active,"
                "COALESCE(SUM(token_limit),0) AS token_limit,"
                "COALESCE(SUM(tokens_used),0) AS tokens_used,"
                "COALESCE(SUM(image_limit),0) AS image_limit,"
                "COALESCE(SUM(images_used),0) AS images_used FROM admin_ops_users"
            ).fetchone()
            return UsageSummaryProjection(
                **dict(row), captured_at=_now()
            )
        finally:
            connection.close()

    def record_provider_usage(
        self,
        *,
        source_service: str,
        source_id: str,
        usage_kind: str,
        account_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        image_count: int = 0,
        provider_created_at: str,
    ) -> AdminUserProjection:
        """Settle one immutable provider fact into account counters exactly once.

        The provider's durable request/job identity is the idempotency key.
        Replaying the same identity and payload is a no-op; reusing it with
        different ownership or usage is a hard conflict. The fact, counters
        and audit chain commit in one SQLite transaction.
        """

        if (
            source_service not in _PROVIDER_USAGE_SERVICES
            or usage_kind not in _PROVIDER_USAGE_KINDS
            or not isinstance(source_id, str)
            or not 1 <= len(source_id) <= 256
            or any(ord(character) < 33 for character in source_id)
            or not isinstance(account_id, str)
            or not 1 <= len(account_id) <= 256
            or any(ord(character) < 33 for character in account_id)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 10**12
                for value in (
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    image_count,
                )
            )
            or total_tokens < input_tokens + output_tokens
            or (
                usage_kind == "chat"
                and (total_tokens <= 0 or image_count != 0)
            )
            or (
                usage_kind == "image"
                and (total_tokens != 0 or image_count <= 0)
            )
            or not isinstance(provider_created_at, str)
            or not 1 <= len(provider_created_at) <= 64
        ):
            raise ValueError("provider usage fact is invalid")
        try:
            created = datetime.fromisoformat(provider_created_at)
        except ValueError:
            raise ValueError("provider usage timestamp is invalid") from None
        if created.tzinfo is None:
            raise ValueError("provider usage timestamp is invalid")
        created_at = created.astimezone(UTC).isoformat()
        material = {
            "source_service": source_service,
            "source_id": source_id,
            "usage_kind": usage_kind,
            "account_id": account_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "image_count": image_count,
            "provider_created_at": created_at,
        }
        payload_sha256 = _sha(material)
        fact_id = "usagefact_" + hashlib.sha256(
            b"ecorex-provider-usage-fact-v1\0"
            + source_service.encode("ascii")
            + b"\0"
            + source_id.encode("utf-8")
            + b"\0"
            + usage_kind.encode("ascii")
        ).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT account_id,payload_sha256 FROM admin_ops_provider_usage_facts "
                "WHERE fact_id=?",
                (fact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["account_id"]) != account_id
                    or str(existing["payload_sha256"]) != payload_sha256
                ):
                    raise AdminManagementConflict(
                        "provider usage identity was reused"
                    )
                row = connection.execute(
                    "SELECT * FROM admin_ops_users WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if row is None:
                    raise AdminManagementNotFound("user does not exist")
                connection.commit()
                return self._user(connection, row)
            row = connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise AdminManagementNotFound("user does not exist")
            revision = int(row["revision"]) + 1
            token_value = int(row["tokens_used"]) + total_tokens
            image_value = int(row["images_used"]) + image_count
            recorded_at = _now()
            connection.execute(
                "INSERT INTO admin_ops_provider_usage_facts("
                "fact_id,source_service,source_id,usage_kind,account_id,"
                "input_tokens,output_tokens,total_tokens,image_count,payload_sha256,"
                "provider_created_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    source_service,
                    source_id,
                    usage_kind,
                    account_id,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    image_count,
                    payload_sha256,
                    created_at,
                    recorded_at,
                ),
            )
            updated = connection.execute(
                "UPDATE admin_ops_users SET tokens_used=?,images_used=?,revision=?,"
                "updated_at=? WHERE account_id=? AND revision=?",
                (
                    token_value,
                    image_value,
                    revision,
                    recorded_at,
                    account_id,
                    int(row["revision"]),
                ),
            )
            if updated.rowcount != 1:
                raise AdminManagementConflict("user revision changed")
            self._audit(
                connection,
                actor=_PROVIDER_USAGE_ACTOR,
                action="usage.provider.settled",
                target_id=account_id,
                payload={
                    "fact_id": fact_id,
                    "source_service": source_service,
                    "source_id_sha256": hashlib.sha256(
                        source_id.encode("utf-8")
                    ).hexdigest(),
                    "usage_kind": usage_kind,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "image_count": image_count,
                    "resulting_user_revision": revision,
                },
            )
            projection = self._user(
                connection,
                connection.execute(
                    "SELECT * FROM admin_ops_users WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            )
            connection.commit()
            return projection
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_model_configurations(self) -> list[ModelConfigurationProjection]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM admin_ops_model_configs "
                "ORDER BY CASE modality WHEN 'chat' THEN 0 WHEN 'image_generation' THEN 1 ELSE 2 END,"
                "created_at,local_model_id"
            ).fetchall()
            return [self._model_configuration(connection, row) for row in rows]
        finally:
            connection.close()

    def create_model_configuration(
        self,
        request: CreateModelConfigurationRequest,
        *,
        actor: ControlPrincipal,
    ) -> ModelConfigurationProjection:
        api_key = request.api_key.get_secret_value()
        request_payload = {
            **request.model_dump(mode="json", exclude={"api_key"}),
            "key_fingerprint": self._secrets.fingerprint(api_key),
        }
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="model.create",
            request_payload=request_payload,
            projection=ModelConfigurationProjection,
            action=lambda connection: self._create_model(
                connection, request, api_key, actor
            ),
        )

    def _create_model(
        self,
        connection: sqlite3.Connection,
        request: CreateModelConfigurationRequest,
        api_key: str,
        actor: ControlPrincipal,
    ) -> ModelConfigurationProjection:
        config_id = "model_" + uuid.uuid4().hex
        secret_id = "msecret_" + uuid.uuid4().hex
        nonce, ciphertext, fingerprint = self._secrets.encrypt(secret_id, api_key)
        now = _now()
        try:
            connection.execute(
                "INSERT INTO admin_ops_model_configs("
                "config_id,local_model_id,modality,active_revision,draft_revision,created_at,updated_at"
                ") VALUES(?,?,?,NULL,1,?,?)",
                (config_id, request.local_model_id, request.modality, now, now),
            )
            connection.execute(
                "INSERT INTO admin_ops_secrets(secret_id,nonce,ciphertext,fingerprint,created_at) "
                "VALUES(?,?,?,?,?)",
                (secret_id, nonce, ciphertext, fingerprint, now),
            )
            connection.execute(
                "INSERT INTO admin_ops_model_revisions("
                "config_id,revision,display_name,upstream_model_id,provider_preset,"
                "is_default,enabled,status,secret_id,key_fingerprint,test_id,test_status,"
                "test_error_code,tested_at,actor_subject,created_at,updated_at,"
                "provider_origin_preset"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'not_tested',NULL,NULL,?,?,?,?)",
                (
                    config_id,
                    1,
                    request.display_name,
                    request.upstream_model_id,
                    request.provider_preset,
                    int(request.is_default),
                    int(request.enabled),
                    "draft",
                    secret_id,
                    fingerprint,
                    actor.subject,
                    now,
                    now,
                    provider_origin_preset_for_slot(request.local_model_id),
                ),
            )
        except sqlite3.IntegrityError:
            raise AdminManagementConflict("model identity already exists") from None
        self._audit(
            connection,
            actor=actor,
            action="model.create",
            target_id=config_id,
            payload={
                "local_model_id": request.local_model_id,
                "modality": request.modality,
                "display_name": request.display_name,
                "upstream_model_id": request.upstream_model_id,
                "provider_preset": request.provider_preset,
                "is_default": request.is_default,
                "enabled": request.enabled,
                "key_fingerprint": fingerprint,
            },
        )
        row = connection.execute(
            "SELECT * FROM admin_ops_model_configs WHERE config_id=?", (config_id,)
        ).fetchone()
        return self._model_configuration(connection, row)

    def stage_model_configuration(
        self,
        config_id: str,
        request: StageModelConfigurationRequest,
        *,
        actor: ControlPrincipal,
    ) -> ModelConfigurationProjection:
        api_key = request.api_key.get_secret_value() if request.api_key else None
        request_payload = {
            **request.model_dump(mode="json", exclude={"api_key"}),
            "config_id": config_id,
            "key_fingerprint": (
                self._secrets.fingerprint(api_key) if api_key is not None else None
            ),
        }
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="model.stage",
            request_payload=request_payload,
            projection=ModelConfigurationProjection,
            action=lambda connection: self._stage_model(
                connection, config_id, request, api_key, actor
            ),
        )

    def _stage_model(
        self,
        connection: sqlite3.Connection,
        config_id: str,
        request: StageModelConfigurationRequest,
        api_key: str | None,
        actor: ControlPrincipal,
    ) -> ModelConfigurationProjection:
        config = connection.execute(
            "SELECT * FROM admin_ops_model_configs WHERE config_id=?", (config_id,)
        ).fetchone()
        if config is None:
            raise AdminManagementNotFound("model configuration does not exist")
        modality = str(config["modality"])
        local_model_id = str(config["local_model_id"])
        if request.provider_preset != provider_protocol_for_slot(local_model_id):
            raise AdminManagementConflict(
                "model provider preset does not match the configured modality"
            )
        active_revision = config["active_revision"]
        if active_revision != request.expected_active_revision:
            raise AdminManagementConflict("active model revision changed")
        previous_draft = config["draft_revision"]
        source_revision = previous_draft or active_revision
        source = None
        if source_revision is not None:
            source = connection.execute(
                "SELECT * FROM admin_ops_model_revisions WHERE config_id=? AND revision=?",
                (config_id, source_revision),
            ).fetchone()
        if api_key is None and source is None:
            raise AdminManagementConflict("model key is required")
        if api_key is None:
            secret_id = str(source["secret_id"])
            fingerprint = str(source["key_fingerprint"])
        else:
            secret_id = "msecret_" + uuid.uuid4().hex
            nonce, ciphertext, fingerprint = self._secrets.encrypt(secret_id, api_key)
            connection.execute(
                "INSERT INTO admin_ops_secrets(secret_id,nonce,ciphertext,fingerprint,created_at) "
                "VALUES(?,?,?,?,?)",
                (secret_id, nonce, ciphertext, fingerprint, _now()),
            )
        revision = int(
            connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM admin_ops_model_revisions "
                "WHERE config_id=?",
                (config_id,),
            ).fetchone()[0]
        )
        now = _now()
        if previous_draft is not None:
            connection.execute(
                "UPDATE admin_ops_model_revisions SET status='superseded',updated_at=? "
                "WHERE config_id=? AND revision=? AND status IN ('draft','testing','rejected')",
                (now, config_id, previous_draft),
            )
        connection.execute(
            "INSERT INTO admin_ops_model_revisions("
            "config_id,revision,display_name,upstream_model_id,provider_preset,is_default,"
            "enabled,status,secret_id,key_fingerprint,test_id,test_status,test_error_code,"
            "tested_at,actor_subject,created_at,updated_at,provider_origin_preset"
            ") VALUES(?,?,?,?,?,?,?,'draft',?,?,NULL,'not_tested',NULL,NULL,?,?,?,?)",
            (
                config_id,
                revision,
                request.display_name,
                request.upstream_model_id,
                request.provider_preset,
                int(request.is_default),
                int(request.enabled),
                secret_id,
                fingerprint,
                actor.subject,
                now,
                now,
                provider_origin_preset_for_slot(local_model_id),
            ),
        )
        connection.execute(
            "UPDATE admin_ops_model_configs SET draft_revision=?,updated_at=? WHERE config_id=?",
            (revision, now, config_id),
        )
        self._audit(
            connection,
            actor=actor,
            action="model.stage",
            target_id=config_id,
            payload={
                "revision": revision,
                "display_name": request.display_name,
                "upstream_model_id": request.upstream_model_id,
                "provider_preset": request.provider_preset,
                "is_default": request.is_default,
                "enabled": request.enabled,
                "key_fingerprint": fingerprint,
            },
        )
        updated = connection.execute(
            "SELECT * FROM admin_ops_model_configs WHERE config_id=?", (config_id,)
        ).fetchone()
        return self._model_configuration(connection, updated)

    def begin_model_test(
        self,
        config_id: str,
        revision: int,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
    ) -> ModelTestLease | ModelTestProjection:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request_sha = _sha({"config_id": config_id, "revision": revision})
            existing = connection.execute(
                "SELECT * FROM admin_ops_model_tests WHERE actor_subject=? "
                "AND client_request_id=?",
                (actor.subject, client_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["config_id"] != config_id
                    or int(existing["revision"]) != revision
                    or existing["request_sha256"] != request_sha
                ):
                    raise AdminManagementConflict("idempotency key was reused")
                config = connection.execute(
                    "SELECT active_revision FROM admin_ops_model_configs WHERE config_id=?",
                    (config_id,),
                ).fetchone()
                projection = ModelTestProjection(
                    test_id=str(existing["test_id"]),
                    config_id=config_id,
                    revision=revision,
                    status=str(existing["status"]),
                    error_code=existing["error_code"],
                    active_revision=(
                        int(config["active_revision"])
                        if config is not None and config["active_revision"] is not None
                        else None
                    ),
                    completed_at=existing["completed_at"],
                )
                connection.commit()
                return projection
            config = connection.execute(
                "SELECT * FROM admin_ops_model_configs WHERE config_id=?", (config_id,)
            ).fetchone()
            if config is None:
                raise AdminManagementNotFound("model configuration does not exist")
            if config["draft_revision"] != revision:
                raise AdminManagementConflict("model draft revision changed")
            row = connection.execute(
                "SELECT revisions.*,configs.local_model_id,configs.modality "
                "FROM admin_ops_model_revisions revisions "
                "JOIN admin_ops_model_configs configs USING(config_id) "
                "WHERE revisions.config_id=? AND revisions.revision=?",
                (config_id, revision),
            ).fetchone()
            if row is None or row["status"] not in {"draft", "rejected"}:
                raise AdminManagementConflict("model revision cannot be tested")
            if row["test_error_code"] == "rotation_required":
                raise AdminManagementConflict("model credential rotation is required")
            test_id = "mtest_" + uuid.uuid4().hex
            now = _now()
            cursor = connection.execute(
                "UPDATE admin_ops_model_revisions SET status='testing',test_id=?,"
                "test_status='running',test_error_code=NULL,tested_at=NULL,updated_at=? "
                "WHERE config_id=? AND revision=? AND status IN ('draft','rejected')",
                (test_id, now, config_id, revision),
            )
            if cursor.rowcount != 1:
                raise AdminManagementConflict("model revision changed")
            connection.execute(
                "INSERT INTO admin_ops_model_tests("
                "test_id,config_id,revision,status,error_code,actor_subject,"
                "client_request_id,request_sha256,started_at,completed_at"
                ") VALUES(?,?,?,'running',NULL,?,?,?,?,NULL)",
                (
                    test_id,
                    config_id,
                    revision,
                    actor.subject,
                    client_request_id,
                    request_sha,
                    now,
                ),
            )
            secret = self._read_secret(connection, str(row["secret_id"]))
            configuration = ActiveModelConfiguration(
                config_id=config_id,
                revision=revision,
                local_model_id=str(row["local_model_id"]),
                modality=str(row["modality"]),  # type: ignore[arg-type]
                display_name=str(row["display_name"]),
                upstream_model_id=str(row["upstream_model_id"]),
                provider_preset=str(row["provider_preset"]),  # type: ignore[arg-type]
                provider_origin_preset=str(row["provider_origin_preset"]),  # type: ignore[arg-type]
                is_default=bool(row["is_default"]),
                api_key=secret,
            )
            self._audit(
                connection,
                actor=actor,
                action="model.test_started",
                target_id=config_id,
                payload={"revision": revision, "test_id": test_id},
            )
            connection.commit()
            return ModelTestLease(test_id=test_id, configuration=configuration)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def finish_model_test(
        self,
        lease: ModelTestLease,
        result: ModelConnectionTestResult,
        *,
        actor: ControlPrincipal,
    ) -> ModelTestProjection:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            config_id = lease.configuration.config_id
            revision = lease.configuration.revision
            config = connection.execute(
                "SELECT * FROM admin_ops_model_configs WHERE config_id=?", (config_id,)
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM admin_ops_model_revisions WHERE config_id=? AND revision=?",
                (config_id, revision),
            ).fetchone()
            if row is None:
                raise AdminManagementNotFound("model revision does not exist")
            now = _now()
            stale = (
                config is None
                or config["draft_revision"] != revision
                or row["status"] != "testing"
                or row["test_id"] != lease.test_id
            )
            if stale:
                connection.execute(
                    "UPDATE admin_ops_model_tests SET status='superseded',error_code=NULL,"
                    "completed_at=? WHERE test_id=? AND status='running'",
                    (now, lease.test_id),
                )
                connection.commit()
                return ModelTestProjection(
                    test_id=lease.test_id,
                    config_id=config_id,
                    revision=revision,
                    status="superseded",
                    error_code=None,
                    active_revision=(
                        int(config["active_revision"])
                        if config is not None and config["active_revision"] is not None
                        else None
                    ),
                    completed_at=now,
                )
            if not result.passed:
                connection.execute(
                    "UPDATE admin_ops_model_revisions SET status='rejected',test_status='failed',"
                    "test_error_code=?,tested_at=?,updated_at=? "
                    "WHERE config_id=? AND revision=? AND test_id=?",
                    (result.error_code, now, now, config_id, revision, lease.test_id),
                )
                connection.execute(
                    "UPDATE admin_ops_model_tests SET status='failed',error_code=?,completed_at=? "
                    "WHERE test_id=? AND status='running'",
                    (result.error_code, now, lease.test_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="model.test_failed",
                    target_id=config_id,
                    payload={
                        "revision": revision,
                        "test_id": lease.test_id,
                        "error_code": result.error_code,
                    },
                )
                connection.commit()
                return ModelTestProjection(
                    test_id=lease.test_id,
                    config_id=config_id,
                    revision=revision,
                    status="failed",
                    error_code=result.error_code,
                    active_revision=(
                        int(config["active_revision"])
                        if config["active_revision"] is not None
                        else None
                    ),
                    completed_at=now,
                )
            previous = config["active_revision"]
            if previous is not None and previous != revision:
                connection.execute(
                    "UPDATE admin_ops_model_revisions SET status='superseded',updated_at=? "
                    "WHERE config_id=? AND revision=? AND status='active'",
                    (now, config_id, previous),
                )
            connection.execute(
                "UPDATE admin_ops_model_revisions SET status='active',test_status='passed',"
                "test_error_code=NULL,tested_at=?,updated_at=? "
                "WHERE config_id=? AND revision=? AND test_id=? AND status='testing'",
                (now, now, config_id, revision, lease.test_id),
            )
            connection.execute(
                "UPDATE admin_ops_model_configs SET active_revision=?,draft_revision=NULL,updated_at=? "
                "WHERE config_id=? AND draft_revision=?",
                (revision, now, config_id, revision),
            )
            if bool(row["enabled"]):
                if bool(row["is_default"]):
                    connection.execute(
                        "INSERT INTO admin_ops_model_defaults(modality,config_id,revision,updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(modality) DO UPDATE SET "
                        "config_id=excluded.config_id,revision=excluded.revision,updated_at=excluded.updated_at",
                        (lease.configuration.modality, config_id, revision, now),
                    )
                else:
                    connection.execute(
                        "DELETE FROM admin_ops_model_defaults WHERE modality=? AND config_id=?",
                        (lease.configuration.modality, config_id),
                    )
            else:
                connection.execute(
                    "DELETE FROM admin_ops_model_defaults WHERE config_id=?", (config_id,)
                )
            connection.execute(
                "UPDATE admin_ops_model_tests SET status='passed',error_code=NULL,completed_at=? "
                "WHERE test_id=? AND status='running'",
                (now, lease.test_id),
            )
            self._audit(
                connection,
                actor=actor,
                action="model.activated",
                target_id=config_id,
                payload={
                    "revision": revision,
                    "test_id": lease.test_id,
                    "previous_revision": previous,
                    "key_fingerprint": row["key_fingerprint"],
                },
            )
            connection.commit()
            return ModelTestProjection(
                test_id=lease.test_id,
                config_id=config_id,
                revision=revision,
                status="passed",
                error_code=None,
                active_revision=revision,
                completed_at=now,
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def active_model(
        self, *, local_model_id: str | None = None, modality: str | None = None
    ) -> ActiveModelConfiguration:
        if (local_model_id is None) == (modality is None):
            raise ValueError("select an active model by local ID or modality")
        connection = self._connect()
        try:
            if local_model_id is not None:
                row = connection.execute(
                    "SELECT revisions.*,configs.local_model_id,configs.modality "
                    "FROM admin_ops_model_configs configs "
                    "JOIN admin_ops_model_revisions revisions ON revisions.config_id=configs.config_id "
                    "AND revisions.revision=configs.active_revision "
                    "WHERE configs.local_model_id=? AND revisions.status='active' "
                    "AND revisions.enabled=1",
                    (local_model_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT revisions.*,configs.local_model_id,configs.modality "
                    "FROM admin_ops_model_defaults defaults "
                    "JOIN admin_ops_model_configs configs ON configs.config_id=defaults.config_id "
                    "JOIN admin_ops_model_revisions revisions ON revisions.config_id=defaults.config_id "
                    "AND revisions.revision=defaults.revision "
                    "WHERE defaults.modality=? AND configs.active_revision=defaults.revision "
                    "AND revisions.status='active' AND revisions.enabled=1",
                    (modality,),
                ).fetchone()
            if row is None:
                raise AdminManagementNotFound("active model configuration is unavailable")
            return ActiveModelConfiguration(
                config_id=str(row["config_id"]),
                revision=int(row["revision"]),
                local_model_id=str(row["local_model_id"]),
                modality=str(row["modality"]),  # type: ignore[arg-type]
                display_name=str(row["display_name"]),
                upstream_model_id=str(row["upstream_model_id"]),
                provider_preset=str(row["provider_preset"]),  # type: ignore[arg-type]
                provider_origin_preset=str(row["provider_origin_preset"]),  # type: ignore[arg-type]
                is_default=bool(row["is_default"]),
                api_key=self._read_secret(connection, str(row["secret_id"])),
            )
        finally:
            connection.close()

    def model_revision(
        self, config_id: str, revision: int
    ) -> ActiveModelConfiguration:
        """Resolve a previously tested snapshot for durable in-flight work."""

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT revisions.*,configs.local_model_id,configs.modality "
                "FROM admin_ops_model_revisions revisions "
                "JOIN admin_ops_model_configs configs USING(config_id) "
                "WHERE revisions.config_id=? AND revisions.revision=? "
                "AND revisions.test_status='passed' "
                "AND revisions.status IN ('active','superseded')",
                (config_id, revision),
            ).fetchone()
            if row is None:
                raise AdminManagementNotFound(
                    "tested model configuration revision is unavailable"
                )
            return ActiveModelConfiguration(
                config_id=str(row["config_id"]),
                revision=int(row["revision"]),
                local_model_id=str(row["local_model_id"]),
                modality=str(row["modality"]),  # type: ignore[arg-type]
                display_name=str(row["display_name"]),
                upstream_model_id=str(row["upstream_model_id"]),
                provider_preset=str(row["provider_preset"]),  # type: ignore[arg-type]
                provider_origin_preset=str(row["provider_origin_preset"]),  # type: ignore[arg-type]
                is_default=bool(row["is_default"]),
                api_key=self._read_secret(connection, str(row["secret_id"])),
            )
        finally:
            connection.close()

    def active_public_catalog(self) -> list[dict[str, object]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT configs.config_id,configs.local_model_id,configs.modality,"
                "configs.active_revision,revisions.display_name,revisions.upstream_model_id,"
                "revisions.provider_preset,revisions.enabled,"
                "CASE WHEN defaults.config_id=configs.config_id "
                "AND defaults.revision=configs.active_revision THEN 1 ELSE 0 END AS is_default "
                "FROM admin_ops_model_configs configs "
                "JOIN admin_ops_model_revisions revisions ON revisions.config_id=configs.config_id "
                "AND revisions.revision=configs.active_revision "
                "LEFT JOIN admin_ops_model_defaults defaults ON defaults.modality=configs.modality "
                "WHERE revisions.status='active' AND revisions.enabled=1 "
                "ORDER BY configs.modality,revisions.is_default DESC,configs.local_model_id"
            ).fetchall()
            return [
                {
                    "config_id": str(row["config_id"]),
                    "revision": int(row["active_revision"]),
                    "local_model_id": str(row["local_model_id"]),
                    "modality": str(row["modality"]),
                    "display_name": str(row["display_name"]),
                    "upstream_model_id": str(row["upstream_model_id"]),
                    "provider_preset": str(row["provider_preset"]),
                    "is_default": bool(row["is_default"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def verify_integrity(self) -> None:
        connection = self._connect()
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AdminManagementError("admin management database is corrupt")
            previous = _ZERO_DIGEST
            rows = connection.execute(
                "SELECT * FROM admin_ops_audit ORDER BY sequence"
            ).fetchall()
            for row in rows:
                expected = hashlib.sha256(
                    _canonical(
                        {
                            "sequence": int(row["sequence"]),
                            "actor_subject": str(row["actor_subject"]),
                            "action": str(row["action"]),
                            "target_id": str(row["target_id"]),
                            "payload_sha256": str(row["payload_sha256"]),
                            "previous_digest": previous,
                            "created_at": str(row["created_at"]),
                        }
                    )
                ).hexdigest()
                if row["previous_digest"] != previous or row["entry_digest"] != expected:
                    raise AdminManagementError("admin management audit chain is invalid")
                previous = expected
        finally:
            connection.close()

    def _mutate(
        self,
        *,
        actor: ControlPrincipal,
        client_request_id: str,
        operation: str,
        request_payload: Mapping[str, Any],
        projection: type[Any],
        action: Any,
    ) -> Any:
        request_sha = _sha(request_payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT operation,request_sha256,response_json "
                "FROM admin_ops_idempotency WHERE actor_subject=? AND client_request_id=?",
                (actor.subject, client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["operation"] != operation or existing["request_sha256"] != request_sha:
                    raise AdminManagementConflict("idempotency key was reused")
                response_payload = json.loads(str(existing["response_json"]))
                if (
                    projection is AdminUserProjection
                    and isinstance(response_payload, dict)
                    and "password_configured" not in response_payload
                ):
                    account_id = response_payload.get("account_id")
                    row = connection.execute(
                        "SELECT * FROM admin_ops_users WHERE account_id=?",
                        (account_id,),
                    ).fetchone()
                    if row is None:
                        raise AdminManagementConflict(
                            "idempotent user response no longer exists"
                        )
                    result = self._user(connection, row)
                else:
                    result = projection.model_validate(response_payload)
                connection.commit()
                return result
            result = action(connection)
            response_json = result.model_dump_json()
            connection.execute(
                "INSERT INTO admin_ops_idempotency("
                "actor_subject,client_request_id,operation,request_sha256,response_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    actor.subject,
                    client_request_id,
                    operation,
                    request_sha,
                    response_json,
                    _now(),
                ),
            )
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor: ControlPrincipal,
        action: str,
        target_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT sequence,entry_digest FROM admin_ops_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = (int(row["sequence"]) if row is not None else 0) + 1
        previous = str(row["entry_digest"]) if row is not None else _ZERO_DIGEST
        created_at = _now()
        payload_sha = _sha(payload)
        entry = hashlib.sha256(
            _canonical(
                {
                    "sequence": sequence,
                    "actor_subject": actor.subject,
                    "action": action,
                    "target_id": target_id,
                    "payload_sha256": payload_sha,
                    "previous_digest": previous,
                    "created_at": created_at,
                }
            )
        ).hexdigest()
        connection.execute(
            "INSERT INTO admin_ops_audit("
            "sequence,actor_subject,action,target_id,payload_sha256,previous_digest,entry_digest,created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                sequence,
                actor.subject,
                action,
                target_id,
                payload_sha,
                previous,
                entry,
                created_at,
            ),
        )

    def _read_secret(self, connection: sqlite3.Connection, secret_id: str) -> str:
        row = connection.execute(
            "SELECT nonce,ciphertext FROM admin_ops_secrets WHERE secret_id=?",
            (secret_id,),
        ).fetchone()
        if row is None:
            raise AdminModelSecretError("managed model key is missing")
        return self._secrets.decrypt(secret_id, row["nonce"], row["ciphertext"])

    @staticmethod
    def _require_available_identity_namespace(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        email: str | None,
        exclude_account_id: str | None = None,
    ) -> None:
        requested = {account_id.casefold()}
        if email is not None:
            requested.add(email.casefold())
        rows = connection.execute(
            "SELECT account_id,email FROM admin_ops_users "
            "WHERE (? IS NULL OR account_id<>?)",
            (exclude_account_id, exclude_account_id),
        ).fetchall()
        for row in rows:
            occupied = {str(row["account_id"]).casefold()}
            if row["email"] is not None:
                occupied.add(str(row["email"]).casefold())
            if requested & occupied:
                raise AdminManagementConflict("user identity already exists")

    @staticmethod
    def _require_user_or_conflict(
        connection: sqlite3.Connection, account_id: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM admin_ops_users WHERE account_id=?", (account_id,)
        ).fetchone() is None:
            raise AdminManagementNotFound("user does not exist")
        raise AdminManagementConflict("user revision changed")

    @staticmethod
    def _user(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AdminUserProjection:
        credential = connection.execute(
            "SELECT password_changed_at FROM admin_ops_password_credentials "
            "WHERE account_id=?",
            (row["account_id"],),
        ).fetchone()
        password_changed_at = (
            str(credential["password_changed_at"]) if credential is not None else None
        )
        return AdminUserProjection(
            **{
                key: row[key]
                for key in (
                    "account_id",
                    "display_name",
                    "email",
                    "organization_id",
                    "status",
                    "token_limit",
                    "tokens_used",
                    "image_limit",
                    "images_used",
                    "revision",
                    "created_at",
                    "updated_at",
                )
            },
            password_configured=credential is not None,
            credential_state="configured" if credential is not None else "missing",
            password_changed_at=password_changed_at,
        )

    def _model_configuration(
        self, connection: sqlite3.Connection, config: sqlite3.Row
    ) -> ModelConfigurationProjection:
        active = None
        draft = None
        if config["active_revision"] is not None:
            active = self._model_revision(
                connection, str(config["config_id"]), int(config["active_revision"])
            )
        if config["draft_revision"] is not None:
            draft = self._model_revision(
                connection, str(config["config_id"]), int(config["draft_revision"])
            )
        return ModelConfigurationProjection(
            config_id=str(config["config_id"]), active=active, draft=draft
        )

    @staticmethod
    def _model_revision(
        connection: sqlite3.Connection, config_id: str, revision: int
    ) -> ModelRevisionProjection:
        row = connection.execute(
            "SELECT revisions.*,configs.local_model_id,configs.modality "
            "FROM admin_ops_model_revisions revisions "
            "JOIN admin_ops_model_configs configs USING(config_id) "
            "WHERE revisions.config_id=? AND revisions.revision=?",
            (config_id, revision),
        ).fetchone()
        if row is None:
            raise AdminManagementError("model revision pointer is invalid")
        return ModelRevisionProjection(
            config_id=config_id,
            revision=revision,
            local_model_id=str(row["local_model_id"]),
            modality=str(row["modality"]),
            display_name=str(row["display_name"]),
            upstream_model_id=str(row["upstream_model_id"]),
            provider_preset=str(row["provider_preset"]),
            is_default=bool(row["is_default"]),
            enabled=bool(row["enabled"]),
            status=str(row["status"]),
            key_configured=True,
            key_fingerprint=str(row["key_fingerprint"]),
            test_id=row["test_id"],
            test_status=str(row["test_status"]),
            test_error_code=row["test_error_code"],
            tested_at=row["tested_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


__all__ = [
    "AdminManagementConflict",
    "AdminManagementError",
    "AdminManagementNotFound",
    "AdminManagementRepository",
    "AdminPasswordAuthenticationError",
    "AdminPasswordLocked",
    "AdminModelSecretError",
    "HTTPSModelConnectionTester",
    "ModelConnectionTester",
    "ModelConnectionTestResult",
    "ModelTestLease",
    "RejectingModelConnectionTester",
]

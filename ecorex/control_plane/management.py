"""Transactional product administration for users, usage and managed models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
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


_ZERO_DIGEST = "0" * 64


class AdminManagementError(RuntimeError):
    pass


class AdminManagementConflict(AdminManagementError):
    pass


class AdminManagementNotFound(AdminManagementError):
    pass


class AdminModelSecretError(AdminManagementError):
    pass


@dataclass(frozen=True, slots=True)
class ModelTestLease:
    test_id: str
    configuration: ActiveModelConfiguration


class _ModelSecretCipher:
    _DOMAIN = b"ecorex-admin-model-secret-v1\0"

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("managed model encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)

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
                items=[self._user(row) for row in rows],
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
            return self._user(row)
        finally:
            connection.close()

    def create_user(
        self, request: CreateAdminUserRequest, *, actor: ControlPrincipal
    ) -> AdminUserProjection:
        public_request = request.model_dump(mode="json")
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="user.create",
            request_payload=public_request,
            projection=AdminUserProjection,
            action=lambda connection: self._create_user(connection, request, actor),
        )

    def _create_user(
        self,
        connection: sqlite3.Connection,
        request: CreateAdminUserRequest,
        actor: ControlPrincipal,
    ) -> AdminUserProjection:
        now = _now()
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
        self._audit(
            connection,
            actor=actor,
            action="user.create",
            target_id=request.account_id,
            payload={key: value for key, value in request.model_dump(mode="json").items() if key != "client_request_id"},
        )
        return self._user(
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
        payload = request.model_dump(mode="json") | {"account_id": account_id}
        return self._mutate(
            actor=actor,
            client_request_id=request.client_request_id,
            operation="user.update",
            request_payload=payload,
            projection=AdminUserProjection,
            action=lambda connection: self._update_user(
                connection, account_id, request, actor
            ),
        )

    def _update_user(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        request: UpdateAdminUserRequest,
        actor: ControlPrincipal,
    ) -> AdminUserProjection:
        now = _now()
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
        self._audit(
            connection,
            actor=actor,
            action="user.update",
            target_id=account_id,
            payload={key: value for key, value in request.model_dump(mode="json").items() if key != "client_request_id"},
        )
        return self._user(
            connection.execute(
                "SELECT * FROM admin_ops_users WHERE account_id=?", (account_id,)
            ).fetchone()
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
                result = projection.model_validate_json(existing["response_json"])
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
    def _require_user_or_conflict(
        connection: sqlite3.Connection, account_id: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM admin_ops_users WHERE account_id=?", (account_id,)
        ).fetchone() is None:
            raise AdminManagementNotFound("user does not exist")
        raise AdminManagementConflict("user revision changed")

    @staticmethod
    def _user(row: sqlite3.Row) -> AdminUserProjection:
        return AdminUserProjection(**dict(row))

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
    "AdminModelSecretError",
    "HTTPSModelConnectionTester",
    "ModelConnectionTester",
    "ModelConnectionTestResult",
    "ModelTestLease",
    "RejectingModelConnectionTester",
]

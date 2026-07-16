"""Copy-on-write v0.2.9.2 Admin data import into the v1 management store."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ecorex.control_plane.management_models import (
    MANAGED_MODEL_ORIGIN_PRESETS,
    MANAGED_MODEL_PROVIDER_PROTOCOLS,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.managed_model_policy import MANAGED_CHAT_MODEL_POLICIES


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MODEL_SECRET_DOMAIN = b"ecorex-admin-model-secret-v1\0"
_ROTATION_REQUIRED_ORIGINS = frozenset(
    {"ecorex_chat", "gemini_chat", "ecorex_image"}
)
_MAX_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
_SOURCE_TABLES = frozenset(
    {"users", "client_sessions", "usage_events", "model_credentials"}
)
_SOURCE_COLUMNS = {
    "users": frozenset(
        {
            "id",
            "name",
            "email",
            "role",
            "status",
            "daily_token_limit",
            "weekly_token_limit",
            "created_at",
            "updated_at",
            "deleted_at",
        }
    ),
    "client_sessions": frozenset(
        {"id", "user_id", "expires_at", "revoked_at"}
    ),
    "usage_events": frozenset(
        {
            "id",
            "category",
            "amount",
            "user_email",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "created_at",
        }
    ),
    "model_credentials": frozenset(
        {
            "id",
            "name",
            "provider",
            "model",
            "bot_type",
            "api_key",
            "scope_type",
            "scope_value",
            "enabled",
            "created_at",
            "updated_at",
        }
    ),
}


class LegacyAdminManagementImportError(RuntimeError):
    """The source, target or migration identity is unsafe or incompatible."""


@dataclass(frozen=True, slots=True)
class LegacyAdminManagementImportReport:
    schema_version: int
    source_version: str
    dry_run: bool
    already_imported: bool
    source_file_sha256: str
    source_snapshot_sha256: str
    users_imported: int
    active_users: int
    suspended_users: int
    excluded_deleted_users: int
    excluded_unsupported_users: int
    eligible_sessions: int
    excluded_revoked_sessions: int
    excluded_expired_sessions: int
    usage_events_aggregated: int
    usage_events_excluded: int
    model_slots_imported: int
    model_slots_pending_test: int
    excluded_model_credentials: int
    import_receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _User:
    account_id: str
    display_name: str
    email: str
    status: str
    token_limit: int
    tokens_used: int
    images_used: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class _Model:
    local_model_id: str
    modality: str
    display_name: str
    upstream_model_id: str
    provider_preset: str
    provider_origin_preset: str
    is_default: bool
    api_key: str


def import_v0292_admin_management(
    source_database: str | os.PathLike[str],
    target_database: str | os.PathLike[str],
    *,
    encryption_key: bytes | None,
    dry_run: bool = False,
    as_of: datetime | None = None,
) -> LegacyAdminManagementImportReport:
    source = _regular_database(source_database, "legacy Admin")
    target_input = Path(target_database).expanduser()
    if target_input.is_symlink():
        raise LegacyAdminManagementImportError("v1 management target is unavailable")
    target = target_input.resolve()
    if source == target:
        raise LegacyAdminManagementImportError("legacy source and v1 target must differ")
    if not dry_run:
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise LegacyAdminManagementImportError("v1 encryption authority is unavailable")
        try:
            AdminManagementSchemaManager(target).validate()
        except Exception:
            raise LegacyAdminManagementImportError(
                "v1 management target is unavailable"
            ) from None
    selected_cutoff = as_of or datetime.now(UTC)
    if selected_cutoff.tzinfo is None:
        raise LegacyAdminManagementImportError("migration cutoff is invalid")
    cutoff = selected_cutoff.astimezone(UTC).replace(microsecond=0)
    file_sha = _file_sha256(source)
    source_connection = _connect_source(source)
    try:
        data_version_before = int(
            source_connection.execute("PRAGMA data_version").fetchone()[0]
        )
        source_connection.execute("BEGIN")
        _validate_source_schema(source_connection)
        users_rows = source_connection.execute(
            "SELECT id,name,email,role,status,daily_token_limit,weekly_token_limit,"
            "created_at,updated_at,deleted_at FROM users ORDER BY id"
        ).fetchall()
        session_rows = source_connection.execute(
            "SELECT id,user_id,expires_at,revoked_at FROM client_sessions ORDER BY id"
        ).fetchall()
        usage_rows = source_connection.execute(
            "SELECT id,category,amount,user_email,input_tokens,output_tokens,"
            "total_tokens,created_at FROM usage_events ORDER BY id"
        ).fetchall()
        model_rows = source_connection.execute(
            "SELECT id,name,provider,model,bot_type,api_key,scope_type,scope_value,"
            "enabled,created_at,updated_at FROM model_credentials ORDER BY id"
        ).fetchall()
        source_connection.commit()
        data_version_after = int(
            source_connection.execute("PRAGMA data_version").fetchone()[0]
        )
    except sqlite3.Error:
        if source_connection.in_transaction:
            source_connection.rollback()
        raise LegacyAdminManagementImportError(
            "legacy Admin snapshot could not be read"
        ) from None
    finally:
        source_connection.close()

    if data_version_before != data_version_after or file_sha != _file_sha256(source):
        raise LegacyAdminManagementImportError(
            "legacy Admin source changed during inventory"
        )

    snapshot_sha = _snapshot_sha256(
        users_rows, session_rows, usage_rows, model_rows
    )
    users, user_counts = _users(users_rows, usage_rows)
    session_counts = _sessions(session_rows, users, cutoff=cutoff)
    models, excluded_models = _models(model_rows)
    receipt_material = {
        "source_version": "0.2.9.2",
        "source_file_sha256": file_sha,
        "source_snapshot_sha256": snapshot_sha,
        "users": [user.account_id for user in users],
        "model_slots": [model.local_model_id for model in models],
    }
    receipt_sha = hashlib.sha256(
        b"ecorex-v0292-admin-management-import-v1\n"
        + _canonical(receipt_material)
    ).hexdigest()
    report = LegacyAdminManagementImportReport(
        schema_version=1,
        source_version="0.2.9.2",
        dry_run=dry_run,
        already_imported=False,
        source_file_sha256=file_sha,
        source_snapshot_sha256=snapshot_sha,
        users_imported=len(users),
        active_users=user_counts["active"],
        suspended_users=user_counts["suspended"],
        excluded_deleted_users=user_counts["deleted"],
        excluded_unsupported_users=user_counts["unsupported"],
        eligible_sessions=session_counts["eligible"],
        excluded_revoked_sessions=session_counts["revoked"],
        excluded_expired_sessions=session_counts["expired"],
        usage_events_aggregated=user_counts["usage_aggregated"],
        usage_events_excluded=user_counts["usage_excluded"],
        model_slots_imported=len(models),
        model_slots_pending_test=len(models),
        excluded_model_credentials=excluded_models,
        import_receipt_sha256=receipt_sha,
    )
    if dry_run:
        return report
    assert encryption_key is not None
    return _commit(target, encryption_key, users, models, report)


def _commit(
    target: Path,
    encryption_key: bytes,
    users: tuple[_User, ...],
    models: tuple[_Model, ...],
    report: LegacyAdminManagementImportReport,
) -> LegacyAdminManagementImportReport:
    connection = sqlite3.connect(target, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        operation = "legacy.v0292.admin-management.import"
        actor = "migration:v0.2.9.2"
        request_id = "legacy-admin-" + report.import_receipt_sha256[:32]
        existing = connection.execute(
            "SELECT operation,response_json FROM admin_ops_idempotency "
            "WHERE actor_subject=? AND client_request_id=?",
            (actor, request_id),
        ).fetchone()
        if existing is not None:
            if existing["operation"] != operation:
                raise LegacyAdminManagementImportError(
                    "legacy import identity conflicts with existing state"
                )
            previous = LegacyAdminManagementImportReport(
                **json.loads(str(existing["response_json"]))
            )
            connection.commit()
            return LegacyAdminManagementImportReport(
                **{
                    **previous.to_dict(),
                    "dry_run": False,
                    "already_imported": True,
                }
            )
        occupied = sum(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("admin_ops_users", "admin_ops_model_configs")
        )
        if occupied:
            raise LegacyAdminManagementImportError(
                "v1 management target already contains business data"
            )
        now = datetime.now(UTC).isoformat()
        for user in users:
            connection.execute(
                "INSERT INTO admin_ops_users("
                "account_id,display_name,email,organization_id,status,token_limit,"
                "tokens_used,image_limit,images_used,revision,created_at,updated_at) "
                "VALUES(?,?,?,NULL,?,?,?,?,?,1,?,?)",
                (
                    user.account_id,
                    user.display_name,
                    user.email,
                    user.status,
                    user.token_limit,
                    user.tokens_used,
                    user.images_used,
                    user.images_used,
                    user.created_at or now,
                    user.updated_at or now,
                ),
            )
        cipher = AESGCM(encryption_key)
        for model in models:
            rotation_required = (
                model.provider_origin_preset in _ROTATION_REQUIRED_ORIGINS
            )
            config_id = "legacy_model_" + hashlib.sha256(
                model.local_model_id.encode("utf-8")
            ).hexdigest()[:24]
            secret_id = "legacy_secret_" + uuid.uuid4().hex
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(
                nonce,
                model.api_key.encode("utf-8"),
                _MODEL_SECRET_DOMAIN + secret_id.encode("ascii"),
            )
            fingerprint = hashlib.sha256(model.api_key.encode("utf-8")).hexdigest()[:16]
            connection.execute(
                "INSERT INTO admin_ops_model_configs("
                "config_id,local_model_id,modality,active_revision,draft_revision,"
                "created_at,updated_at) VALUES(?,?,?,NULL,1,?,?)",
                (config_id, model.local_model_id, model.modality, now, now),
            )
            connection.execute(
                "INSERT INTO admin_ops_secrets("
                "secret_id,nonce,ciphertext,fingerprint,created_at) VALUES(?,?,?,?,?)",
                (secret_id, nonce, ciphertext, fingerprint, now),
            )
            connection.execute(
                "INSERT INTO admin_ops_model_revisions("
                "config_id,revision,display_name,upstream_model_id,provider_preset,"
                "is_default,enabled,status,secret_id,key_fingerprint,test_id,test_status,"
                "test_error_code,tested_at,actor_subject,created_at,updated_at,"
                "provider_origin_preset) "
                "VALUES(?,?,?,?,?,?,?,'draft',?,?,NULL,?,?,NULL,?,?,?,?)",
                (
                    config_id,
                    1,
                    model.display_name,
                    model.upstream_model_id,
                    model.provider_preset,
                    int(model.is_default),
                    0 if rotation_required else 1,
                    secret_id,
                    fingerprint,
                    "failed" if rotation_required else "not_tested",
                    "rotation_required" if rotation_required else None,
                    actor,
                    now,
                    now,
                    model.provider_origin_preset,
                ),
            )
        payload = report.to_dict()
        response_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_sha = hashlib.sha256(
            _canonical(
                {
                    "source_snapshot_sha256": report.source_snapshot_sha256,
                    "import_receipt_sha256": report.import_receipt_sha256,
                }
            )
        ).hexdigest()
        connection.execute(
            "INSERT INTO admin_ops_idempotency("
            "actor_subject,client_request_id,operation,request_sha256,response_json,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (actor, request_id, operation, request_sha, response_json, now),
        )
        _append_audit(
            connection,
            actor=actor,
            target_id=report.import_receipt_sha256,
            payload_sha256=request_sha,
            created_at=now,
        )
        connection.commit()
        return report
    except LegacyAdminManagementImportError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise LegacyAdminManagementImportError(
            "legacy Admin import could not be committed"
        ) from None
    finally:
        connection.close()


def _append_audit(
    connection: sqlite3.Connection,
    *,
    actor: str,
    target_id: str,
    payload_sha256: str,
    created_at: str,
) -> None:
    row = connection.execute(
        "SELECT sequence,entry_digest FROM admin_ops_audit ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = (int(row["sequence"]) if row is not None else 0) + 1
    previous = str(row["entry_digest"]) if row is not None else "0" * 64
    entry = hashlib.sha256(
        _canonical(
            {
                "sequence": sequence,
                "actor_subject": actor,
                "action": "legacy.admin_management.imported",
                "target_id": target_id,
                "payload_sha256": payload_sha256,
                "previous_digest": previous,
                "created_at": created_at,
            }
        )
    ).hexdigest()
    connection.execute(
        "INSERT INTO admin_ops_audit("
        "sequence,actor_subject,action,target_id,payload_sha256,previous_digest,"
        "entry_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            sequence,
            actor,
            "legacy.admin_management.imported",
            target_id,
            payload_sha256,
            previous,
            entry,
            created_at,
        ),
    )


def _users(
    rows: list[sqlite3.Row], usage_rows: list[sqlite3.Row]
) -> tuple[tuple[_User, ...], dict[str, int]]:
    eligible_emails = {
        str(row["email"] or "").strip().casefold()
        for row in rows
        if row["deleted_at"] is None
        and str(row["status"] or "").casefold()
        in {"active", "suspended", "disabled"}
    }
    usage: dict[str, tuple[int, int]] = {}
    aggregated = 0
    excluded_usage = 0
    for row in usage_rows:
        email = str(row["user_email"] or "").strip().casefold()
        if not email or email not in eligible_emails:
            excluded_usage += 1
            continue
        amount = _nonnegative(row["amount"], "legacy usage")
        total = _nonnegative(row["total_tokens"], "legacy usage")
        category = str(row["category"] or "").strip().casefold()
        is_image = category in {"image", "imagegen", "image_generation"}
        token_value = total if total > 0 else (0 if is_image else amount)
        image_value = amount if is_image else 0
        old = usage.get(email, (0, 0))
        usage[email] = (old[0] + token_value, old[1] + image_value)
        aggregated += 1
    users: list[_User] = []
    counts = {
        "active": 0,
        "suspended": 0,
        "deleted": 0,
        "unsupported": 0,
        "usage_aggregated": aggregated,
        "usage_excluded": excluded_usage,
    }
    emails: set[str] = set()
    for row in rows:
        if row["deleted_at"] is not None:
            counts["deleted"] += 1
            continue
        source_status = str(row["status"] or "").casefold()
        if source_status == "active":
            status = "active"
        elif source_status in {"suspended", "disabled"}:
            status = "suspended"
        else:
            counts["unsupported"] += 1
            continue
        account_id = str(row["id"] or "")
        if _SAFE_ID.fullmatch(account_id) is None:
            raise LegacyAdminManagementImportError("legacy user identity is invalid")
        email = _email(row["email"])
        if email in emails:
            raise LegacyAdminManagementImportError("legacy user email is duplicated")
        emails.add(email)
        daily = _nonnegative(row["daily_token_limit"], "legacy quota")
        weekly = _nonnegative(row["weekly_token_limit"], "legacy quota")
        token_limit = weekly if weekly > 0 else daily
        used = usage.get(email, (0, 0))
        users.append(
            _User(
                account_id=account_id,
                display_name=_text(row["name"], 128, "legacy user name"),
                email=email,
                status=status,
                token_limit=token_limit,
                tokens_used=used[0],
                images_used=used[1],
                created_at=_timestamp(row["created_at"], "legacy user timestamp"),
                updated_at=_timestamp(row["updated_at"], "legacy user timestamp"),
            )
        )
        counts[status] += 1
    return tuple(users), counts


def _sessions(
    rows: list[sqlite3.Row], users: tuple[_User, ...], *, cutoff: datetime
) -> dict[str, int]:
    allowed = {user.account_id for user in users}
    counts = {"eligible": 0, "revoked": 0, "expired": 0}
    for row in rows:
        if str(row["user_id"]) not in allowed:
            continue
        if row["revoked_at"] is not None:
            counts["revoked"] += 1
            continue
        expiry = datetime.fromisoformat(
            _timestamp(row["expires_at"], "legacy session timestamp")
        ).astimezone(UTC)
        if expiry <= cutoff:
            counts["expired"] += 1
        else:
            counts["eligible"] += 1
    return counts


def _models(rows: list[sqlite3.Row]) -> tuple[tuple[_Model, ...], int]:
    output: dict[str, _Model] = {}
    excluded = 0
    for row in rows:
        if int(row["enabled"] or 0) != 1 or str(row["scope_type"]) != "global":
            excluded += 1
            continue
        provider = str(row["provider"] or "").strip().casefold()
        bot_type = str(row["bot_type"] or "").strip().casefold()
        upstream = str(row["model"] or "").strip()
        is_image = bot_type in {"image", "imagegen", "image_generation", "image_edit"} or (
            "image" in upstream.casefold()
        )
        slots: tuple[tuple[str, str], ...]
        if is_image:
            slots = (("gpt-image-2", "image_generation"), ("gpt-image-2-edit", "image_edit"))
        elif provider == "openai":
            slots = (("ecorex-chat", "chat"),)
        elif provider == "deepseek":
            slots = (("ecorex-deepseek-v4-pro", "chat"),)
        elif provider == "gemini":
            slots = (("ecorex-gemini-3.1-pro", "chat"),)
        elif provider in {"doubao", "ark"}:
            slots = (("ecorex-doubao-seed-2.0-pro", "chat"),)
        else:
            excluded += 1
            continue
        api_key = _api_key(row["api_key"])
        for local_model_id, modality in slots:
            if local_model_id in output:
                raise LegacyAdminManagementImportError(
                    "legacy model slot is configured more than once"
                )
            if local_model_id == "ecorex-chat":
                model_id = "gpt-5.6-sol"
            elif modality != "chat":
                model_id = "gpt-image-2"
            else:
                model_id = upstream
            if _SAFE_MODEL.fullmatch(model_id) is None:
                raise LegacyAdminManagementImportError("legacy model identity is invalid")
            display = (
                MANAGED_CHAT_MODEL_POLICIES[local_model_id].display_name
                if modality == "chat"
                else ("Image 2" if modality == "image_generation" else "Image 2 精修")
            )
            output[local_model_id] = _Model(
                local_model_id=local_model_id,
                modality=modality,
                display_name=display,
                upstream_model_id=model_id,
                provider_preset=MANAGED_MODEL_PROVIDER_PROTOCOLS[local_model_id],
                provider_origin_preset=MANAGED_MODEL_ORIGIN_PRESETS[local_model_id],
                is_default=local_model_id in {"ecorex-chat", "gpt-image-2", "gpt-image-2-edit"},
                api_key=api_key,
            )
    # The released v0.2.9.2 Admin commonly stored only four chat rows.  Its
    # OpenAI catalog/key nevertheless also authorized gpt-image-2.  Preserve
    # the v1 image selector by deriving both image drafts only when no explicit
    # enabled global image credential won above.  Explicit image credentials
    # always take precedence and duplicate explicit rows already fail closed.
    primary = output.get("ecorex-chat")
    if primary is not None and "gpt-image-2" not in output:
        for local_model_id, modality, display in (
            ("gpt-image-2", "image_generation", "Image 2"),
            ("gpt-image-2-edit", "image_edit", "Image 2 精修"),
        ):
            output[local_model_id] = _Model(
                local_model_id=local_model_id,
                modality=modality,
                display_name=display,
                upstream_model_id="gpt-image-2",
                provider_preset=MANAGED_MODEL_PROVIDER_PROTOCOLS[local_model_id],
                provider_origin_preset=MANAGED_MODEL_ORIGIN_PRESETS[local_model_id],
                is_default=True,
                api_key=primary.api_key,
            )
    return tuple(output[key] for key in sorted(output)), excluded


def _validate_source_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }
    if not _SOURCE_TABLES <= tables:
        raise LegacyAdminManagementImportError("legacy Admin schema is unsupported")
    for table, required in _SOURCE_COLUMNS.items():
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not required <= columns:
            raise LegacyAdminManagementImportError("legacy Admin schema is unsupported")


def _connect_source(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&nofollow=1",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _snapshot_sha256(*groups: list[sqlite3.Row]) -> str:
    digest = hashlib.sha256(b"ecorex-v0292-admin-snapshot-v1\n")
    for rows in groups:
        for row in rows:
            digest.update(_canonical(dict(row)))
            digest.update(b"\n")
    return digest.hexdigest()


def _regular_database(value: str | os.PathLike[str], label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise LegacyAdminManagementImportError(f"{label} database is unavailable")
    path = candidate.resolve()
    try:
        metadata = path.lstat()
    except OSError:
        raise LegacyAdminManagementImportError(f"{label} database is unavailable") from None
    if path.is_symlink() or not path.is_file() or metadata.st_size > _MAX_SOURCE_BYTES:
        raise LegacyAdminManagementImportError(f"{label} database is unavailable")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Mapping[str, Any] | dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _text(value: Any, maximum: int, label: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise LegacyAdminManagementImportError(f"{label} is invalid")
    return result


def _email(value: Any) -> str:
    result = _text(value, 254, "legacy user email").casefold()
    if result.count("@") != 1 or any(char.isspace() for char in result):
        raise LegacyAdminManagementImportError("legacy user email is invalid")
    return result


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
        raise LegacyAdminManagementImportError(f"{label} is invalid")
    return value


def _timestamp(value: Any, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise LegacyAdminManagementImportError(f"{label} is invalid") from None
    if parsed.tzinfo is None:
        raise LegacyAdminManagementImportError(f"{label} is invalid")
    return parsed.astimezone(UTC).isoformat()


def _api_key(value: Any) -> str:
    key = str(value or "")
    if not 8 <= len(key) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise LegacyAdminManagementImportError("legacy model credential is invalid")
    return key


__all__ = [
    "LegacyAdminManagementImportError",
    "LegacyAdminManagementImportReport",
    "import_v0292_admin_management",
]

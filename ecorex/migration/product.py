"""Product activation bridge for the one-time v0.3 copy-on-write import.

The standalone migrator deliberately publishes a complete data directory.  A
product install already has an empty ``state`` directory, so this bridge adds a
small crash-recoverable directory swap and binds it to the verified candidate
slot.  The legacy source is only ever read and is never used as a rollback
target: a failed v1 activation leaves the v0.3 installation untouched.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from collections.abc import Callable
from typing import Any, Mapping

from ecorex.connectors.vault import CredentialVault

from .cas_authority import CAS_AUTHORITY_NAME
from .errors import MigrationError, TargetConflictError
from .migrator import (
    BACKUP_MANIFEST_NAME,
    INVENTORY_NAME,
    REPORT_NAME,
    TARGET_ARTIFACT_ROOT_NAME,
    TARGET_DATABASE_NAME,
    migrate_v030_to_v1,
)
from .models import MigrationReport
from .path_security import (
    is_within,
    lexical_absolute,
    lstat_identity,
    reject_link_or_reparse,
    secure_directory,
    secure_regular_file,
    stable_read_bytes,
)
from .schema_identity import (
    ImportSchemaIdentity,
    ImportSchemaIdentityError,
    data_generation_id,
    physical_schema_sha256,
)
from .target_authority import report_sha256, verify_target_file_authority


PRODUCT_MIGRATION_PLAN_NAME = "migration/v030-plan.json"
PRODUCT_MIGRATION_RECEIPT_NAME = "migration/v030-activation.json"
PRODUCT_MIGRATION_COMPLETION_NAME = "migration/v030-completed.json"
_PREPARED_NAME = "v030-imported-state"
_QUARANTINE_KEY_REFERENCE = "ecorex/migration/v030-quarantine"
_SAFE_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,239}$")
_SAFE_TRANSACTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_MIGRATION_ID = re.compile(r"^mig_[0-9a-f]{26}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PLAN_BYTES = 64 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_COMPLETION_DOMAIN = b"EcoreX v0.3 product migration completion v1\0"
_PLAN_DOMAIN = b"EcoreX v0.3 product migration plan v1\0"
_SOURCE_ROOT_DOMAIN = b"EcoreX canonical legacy source root v1\0"
_TARGET_AUTHORITY_KEY = "__ecorex_target_file_authority"
_TARGET_AUTHORITY_FIELDS = {
    "report_sha256",
    "source_inventory_file_sha256",
    "backup_manifest_sha256",
    "cas_authority_sha256",
    "quarantine_sha256",
}


class ProductMigrationError(MigrationError):
    """The installer-owned migration plan or directory swap is inconsistent."""


@dataclass(frozen=True, slots=True)
class ProductMigrationPlan:
    source_root: str
    source_root_identity_sha256: str
    conversation_database: str | None = None
    memory_database: str | None = None
    config_file: str | None = None
    mcp_file: str | None = None
    ui_state_file: str | None = None
    skills_config_file: str | None = None
    permission_file: str | None = None
    release_evidence_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_version": "0.3.0",
            "target_version": "1.0.0",
            "source_root": self.source_root,
            "source_root_identity_sha256": self.source_root_identity_sha256,
            "conversation_database": self.conversation_database,
            "memory_database": self.memory_database,
            "config_file": self.config_file,
            "mcp_file": self.mcp_file,
            "ui_state_file": self.ui_state_file,
            "skills_config_file": self.skills_config_file,
            "permission_file": self.permission_file,
            "release_evidence_file": self.release_evidence_file,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProductMigrationPlan":
        expected = {
            "schema_version",
            "source_version",
            "target_version",
            "source_root",
            "source_root_identity_sha256",
            "conversation_database",
            "memory_database",
            "config_file",
            "mcp_file",
            "ui_state_file",
            "skills_config_file",
            "permission_file",
            "release_evidence_file",
        }
        if (
            set(raw) != expected
            or raw.get("schema_version") != 1
            or raw.get("source_version") != "0.3.0"
            or raw.get("target_version") != "1.0.0"
        ):
            raise ProductMigrationError("legacy migration plan contract is invalid")
        source_root = raw.get("source_root")
        optional = expected - {
            "schema_version",
            "source_version",
            "target_version",
            "source_root",
            "source_root_identity_sha256",
        }
        source_identity = raw.get("source_root_identity_sha256")
        if (
            not isinstance(source_root, str)
            or not source_root
            or "\x00" in source_root
            or not isinstance(source_identity, str)
            or _HEX_64.fullmatch(source_identity) is None
        ):
            raise ProductMigrationError("legacy migration source is invalid")
        if any(
            raw.get(key) is not None
            and (
                not isinstance(raw.get(key), str)
                or not str(raw.get(key))
                or "\x00" in str(raw.get(key))
            )
            for key in optional
        ):
            raise ProductMigrationError("legacy migration metadata path is invalid")
        try:
            return cls(
                source_root=source_root,
                source_root_identity_sha256=source_identity,
                conversation_database=raw.get("conversation_database"),
                memory_database=raw.get("memory_database"),
                config_file=raw.get("config_file"),
                mcp_file=raw.get("mcp_file"),
                ui_state_file=raw.get("ui_state_file"),
                skills_config_file=raw.get("skills_config_file"),
                permission_file=raw.get("permission_file"),
                release_evidence_file=raw.get("release_evidence_file"),
            )
        except TypeError as error:  # pragma: no cover - exact fields are checked above
            raise ProductMigrationError("legacy migration plan is malformed") from error


def _regular_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    try:
        payload = stable_read_bytes(path, label=label, maximum=maximum)
        if not payload:
            raise ProductMigrationError(f"{label} is not a trusted regular file")
        value = json.loads(payload)
    except ProductMigrationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, MigrationError):
        raise ProductMigrationError(f"{label} is unreadable") from None
    if not isinstance(value, dict):
        raise ProductMigrationError(f"{label} is invalid")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secure_directory(path.parent, label="migration receipt directory")
    except MigrationError:
        raise ProductMigrationError("migration receipt directory is unsafe") from None
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        raise ProductMigrationError("migration receipt could not be persisted") from None
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source_root_identity(source: Path) -> str:
    normalized = os.path.normcase(str(source.resolve(strict=True)))
    return hashlib.sha256(
        _SOURCE_ROOT_DOMAIN + normalized.encode("utf-8", errors="strict")
    ).hexdigest()


def _plan_sha256(plan: ProductMigrationPlan) -> str:
    return hashlib.sha256(_PLAN_DOMAIN + _canonical_json(plan.to_dict())).hexdigest()


def _completion_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "authority_digest"}
    return hashlib.sha256(_COMPLETION_DOMAIN + _canonical_json(unsigned)).hexdigest()


def _report_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop(_TARGET_AUTHORITY_KEY, None)
    return payload


def _target_authority(value: Mapping[str, Any]) -> dict[str, str | None] | None:
    raw = value.get(_TARGET_AUTHORITY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != _TARGET_AUTHORITY_FIELDS:
        raise ProductMigrationError("migrated target file authority is invalid")
    authority = dict(raw)
    for key in _TARGET_AUTHORITY_FIELDS - {"quarantine_sha256"}:
        observed = authority.get(key)
        if not isinstance(observed, str) or _HEX_64.fullmatch(observed) is None:
            raise ProductMigrationError("migrated target file authority is invalid")
    quarantine = authority.get("quarantine_sha256")
    if quarantine is not None and (
        not isinstance(quarantine, str) or _HEX_64.fullmatch(quarantine) is None
    ):
        raise ProductMigrationError("migrated target file authority is invalid")
    return authority


def write_product_migration_plan(
    install_root: str | Path,
    source_root: str | Path,
    **metadata_paths: str | Path | None,
) -> Path:
    """Persist the installer-owned plan before a first v1 candidate is admitted."""

    try:
        root = secure_directory(install_root, label="v1 install root")
        source = secure_directory(source_root, label="legacy source root")
    except MigrationError:
        raise ProductMigrationError("migration roots must be real directories") from None
    # This must precede creation of ``migration/``.  A v1 install nested under
    # the legacy source would otherwise mutate the source merely by admitting
    # the plan, violating the copy-on-write boundary before dry-run begins.
    if is_within(root, source) or is_within(source, root):
        raise ProductMigrationError("legacy source and v1 install root overlap")
    allowed = {
        "conversation_database",
        "memory_database",
        "config_file",
        "mcp_file",
        "ui_state_file",
        "skills_config_file",
        "permission_file",
        "release_evidence_file",
    }
    if set(metadata_paths) - allowed:
        raise ProductMigrationError("migration plan contains an unknown metadata path")
    normalized = {
        key: (str(Path(value).expanduser()) if value is not None else None)
        for key, value in metadata_paths.items()
    }
    plan = ProductMigrationPlan(
        source_root=str(source),
        source_root_identity_sha256=_source_root_identity(source),
        **normalized,
    )
    destination = root / PRODUCT_MIGRATION_PLAN_NAME
    completion = root / PRODUCT_MIGRATION_COMPLETION_NAME
    if os.path.lexists(completion):
        raise ProductMigrationError("legacy migration is already completed")
    if destination.exists():
        existing = ProductMigrationPlan.from_dict(
            _regular_json(destination, maximum=_MAX_PLAN_BYTES, label="migration plan")
        )
        if existing != plan:
            raise ProductMigrationError("another legacy migration plan already exists")
        return destination
    _atomic_json(destination, plan.to_dict())
    return destination


class ProductLegacyMigrationCoordinator:
    """Run, verify, and atomically select one product-layout v0.3 import."""

    def __init__(
        self,
        install_root: str | Path,
        database_path: str | Path,
        *,
        vault: CredentialVault | None = None,
        fault_hook: Callable[[str], None] | None = None,
        storage_schema_authorizer: Callable[[int, str], bool] | None = None,
    ) -> None:
        try:
            self.root = secure_directory(install_root, label="v1 install root")
        except MigrationError:
            raise ProductMigrationError("v1 install root is unsafe") from None
        self.database_path = lexical_absolute(database_path)
        if not is_within(self.database_path, self.root):
            raise ProductMigrationError("Runtime database is outside the install root") from None
        if self.database_path.name != TARGET_DATABASE_NAME:
            raise ProductMigrationError("Runtime database name does not match import layout")
        self.target_root = self.database_path.parent
        self.plan_path = self.root / PRODUCT_MIGRATION_PLAN_NAME
        self.receipt_path = self.root / PRODUCT_MIGRATION_RECEIPT_NAME
        self.completion_path = self.root / PRODUCT_MIGRATION_COMPLETION_NAME
        self.migration_root = self.receipt_path.parent
        self.prepared_root = self.migration_root / _PREPARED_NAME
        self.vault = vault
        self.fault_hook = fault_hook or (lambda _phase: None)
        self.storage_schema_authorizer = storage_schema_authorizer

    @property
    def has_plan(self) -> bool:
        return os.path.lexists(self.plan_path)

    @property
    def has_completion(self) -> bool:
        return os.path.lexists(self.completion_path)

    def _load_plan(self) -> ProductMigrationPlan | None:
        if not self.has_plan:
            return None
        plan = ProductMigrationPlan.from_dict(
            _regular_json(self.plan_path, maximum=_MAX_PLAN_BYTES, label="migration plan")
        )
        return plan

    def _validate_plan_source(self, plan: ProductMigrationPlan) -> Path:
        try:
            source = secure_directory(plan.source_root, label="legacy migration source")
        except (OSError, MigrationError):
            raise ProductMigrationError("legacy migration source is unavailable") from None
        if _source_root_identity(source) != plan.source_root_identity_sha256:
            raise ProductMigrationError("legacy migration source identity changed")
        if is_within(self.target_root, source) or is_within(source, self.root):
            raise ProductMigrationError("legacy source and v1 target overlap")
        return source

    def _candidate_id(self, candidate_slot: str | Path) -> str:
        try:
            slots = secure_directory(self.root / "slots", label="signed slot store")
            candidate = secure_directory(
                candidate_slot,
                label="migration candidate slot",
                root=slots,
            )
        except MigrationError:
            raise ProductMigrationError(
                "migration candidate is outside the signed slot store"
            ) from None
        if candidate.parent != slots or _SAFE_SLOT.fullmatch(candidate.name) is None:
            raise ProductMigrationError("migration candidate is outside the signed slot store")
        return candidate.name

    @staticmethod
    def _migration_kwargs(plan: ProductMigrationPlan) -> dict[str, Any]:
        return {
            "conversation_database": plan.conversation_database,
            "memory_database": plan.memory_database,
            "config_file": plan.config_file,
            "mcp_file": plan.mcp_file,
            "ui_state_file": plan.ui_state_file,
            "skills_config_file": plan.skills_config_file,
            "permission_file": plan.permission_file,
            "release_evidence_file": plan.release_evidence_file,
        }

    def _receipt(self) -> dict[str, Any] | None:
        if not os.path.lexists(self.receipt_path):
            return None
        value = _regular_json(
            self.receipt_path,
            maximum=_MAX_PLAN_BYTES,
            label="migration activation receipt",
        )
        if (
            set(value)
            != {
                "schema_version",
                "state",
                "slot_id",
                "transaction_id",
                "migration_id",
                "source_root_identity_sha256",
                "source_inventory_digest",
                "plan_sha256",
                "import_layout_version",
                "target_storage_schema_version",
                "target_schema_sha256",
                "data_generation_id",
                "report_sha256",
                "source_inventory_file_sha256",
                "backup_manifest_sha256",
                "cas_authority_sha256",
                "quarantine_sha256",
                "quarantine_entry_count",
                "updated_at",
            }
            or value.get("schema_version") != 2
            or value.get("state")
            not in {
                "dry_run_verified",
                "publishing",
                "prepared",
                "swap_pending",
                "committed",
            }
            or not isinstance(value.get("slot_id"), str)
            or _SAFE_SLOT.fullmatch(value["slot_id"]) is None
            or not isinstance(value.get("migration_id"), str)
            or _MIGRATION_ID.fullmatch(value["migration_id"]) is None
            or not isinstance(value.get("source_inventory_digest"), str)
            or _HEX_64.fullmatch(value["source_inventory_digest"]) is None
            or not isinstance(value.get("source_root_identity_sha256"), str)
            or _HEX_64.fullmatch(value["source_root_identity_sha256"]) is None
            or not isinstance(value.get("plan_sha256"), str)
            or _HEX_64.fullmatch(value["plan_sha256"]) is None
            or isinstance(value.get("quarantine_entry_count"), bool)
            or not isinstance(value.get("quarantine_entry_count"), int)
            or value["quarantine_entry_count"] < 0
            or isinstance(value.get("import_layout_version"), bool)
            or not isinstance(value.get("import_layout_version"), int)
            or value["import_layout_version"] < 1
            or isinstance(value.get("target_storage_schema_version"), bool)
            or not isinstance(value.get("target_storage_schema_version"), int)
            or value["target_storage_schema_version"] < 1
            or not isinstance(value.get("target_schema_sha256"), str)
            or _HEX_64.fullmatch(value["target_schema_sha256"]) is None
            or not isinstance(value.get("data_generation_id"), str)
            or re.fullmatch(r"gen_[0-9a-f]{26}", value["data_generation_id"]) is None
            or (
                value.get("transaction_id") is not None
                and (
                    not isinstance(value.get("transaction_id"), str)
                    or _SAFE_TRANSACTION.fullmatch(value["transaction_id"]) is None
                )
            )
            or not isinstance(value.get("updated_at"), str)
        ):
            raise ProductMigrationError("migration activation receipt is invalid")
        try:
            expected_generation = data_generation_id(
                migration_id=value["migration_id"],
                source_inventory_digest=value["source_inventory_digest"],
                import_layout_version=value["import_layout_version"],
                target_storage_schema_version=value["target_storage_schema_version"],
                target_schema_sha256=value["target_schema_sha256"],
            )
        except ImportSchemaIdentityError as error:
            raise ProductMigrationError("migration activation receipt is invalid") from error
        if value["data_generation_id"] != expected_generation:
            raise ProductMigrationError("migration activation receipt generation is invalid")
        authority_values = {
            key: value.get(key) for key in _TARGET_AUTHORITY_FIELDS
        }
        if value["state"] in {"dry_run_verified", "publishing"}:
            if any(item is not None for item in authority_values.values()):
                raise ProductMigrationError(
                    "migration activation receipt has premature target authority"
                )
        else:
            if any(
                not isinstance(authority_values[key], str)
                or _HEX_64.fullmatch(str(authority_values[key])) is None
                for key in _TARGET_AUTHORITY_FIELDS - {"quarantine_sha256"}
            ):
                raise ProductMigrationError(
                    "migration activation target authority is invalid"
                )
            quarantine_sha = authority_values["quarantine_sha256"]
            if value["quarantine_entry_count"] > 0:
                if (
                    not isinstance(quarantine_sha, str)
                    or _HEX_64.fullmatch(quarantine_sha) is None
                ):
                    raise ProductMigrationError(
                        "migration quarantine authority is invalid"
                    )
            elif quarantine_sha is not None:
                raise ProductMigrationError(
                    "migration quarantine authority is inconsistent"
                )
        return value

    def _write_receipt(
        self,
        *,
        state: str,
        slot_id: str,
        report: MigrationReport | Mapping[str, Any],
        plan: ProductMigrationPlan,
        transaction_id: str | None,
    ) -> None:
        if _SAFE_SLOT.fullmatch(slot_id) is None or (
            transaction_id is not None
            and _SAFE_TRANSACTION.fullmatch(transaction_id) is None
        ):
            raise ProductMigrationError("migration activation identity is invalid")
        if isinstance(report, MigrationReport):
            migration_id = report.migration_id
            source_digest = report.source_inventory_digest
            quarantine_count = report.quarantine_entry_count
            schema_identity = ImportSchemaIdentity(
                import_layout_version=report.import_layout_version,
                target_storage_schema_version=report.storage_schema_version,
                target_schema_sha256=report.target_schema_sha256,
                data_generation_id=report.data_generation_id,
            )
            target_authority = None
        else:
            migration_id = str(report["migration_id"])
            source_digest = str(report["source_inventory_digest"])
            quarantine = report.get("quarantine")
            if "quarantine_entry_count" in report:
                quarantine_count = int(report["quarantine_entry_count"])
            else:
                quarantine_count = int(
                    quarantine.get("entry_count", 0)
                    if isinstance(quarantine, Mapping)
                    else 0
                )
            if "storage_schema_version" in report:
                schema_identity = self._report_schema_identity(report)
            else:
                try:
                    schema_identity = ImportSchemaIdentity(
                        import_layout_version=report["import_layout_version"],
                        target_storage_schema_version=report[
                            "target_storage_schema_version"
                        ],
                        target_schema_sha256=report["target_schema_sha256"],
                        data_generation_id=report["data_generation_id"],
                    )
                except (KeyError, TypeError, ImportSchemaIdentityError) as error:
                    raise ProductMigrationError(
                        "migration receipt schema identity is invalid"
                    ) from error
            target_authority = _target_authority(report)
        if _MIGRATION_ID.fullmatch(migration_id) is None:
            raise ProductMigrationError("migration report identity is invalid")
        _atomic_json(
            self.receipt_path,
            {
                "schema_version": 2,
                "state": state,
                "slot_id": slot_id,
                "transaction_id": transaction_id,
                "migration_id": migration_id,
                "source_root_identity_sha256": plan.source_root_identity_sha256,
                "source_inventory_digest": source_digest,
                "plan_sha256": _plan_sha256(plan),
                "import_layout_version": schema_identity.import_layout_version,
                "target_storage_schema_version": (
                    schema_identity.target_storage_schema_version
                ),
                "target_schema_sha256": schema_identity.target_schema_sha256,
                "data_generation_id": schema_identity.data_generation_id,
                **(
                    target_authority
                    if target_authority is not None
                    else {key: None for key in _TARGET_AUTHORITY_FIELDS}
                ),
                "quarantine_entry_count": quarantine_count,
                "updated_at": datetime.now(UTC).isoformat(timespec="microseconds"),
            },
        )

    def _completion(self) -> dict[str, Any] | None:
        if not self.has_completion:
            return None
        value = _regular_json(
            self.completion_path,
            maximum=_MAX_PLAN_BYTES,
            label="migration completion authority",
        )
        expected_fields = {
            "schema_version",
            "state",
            "source_version",
            "target_version",
            "slot_id",
            "transaction_id",
            "migration_id",
            "source_root_identity_sha256",
            "source_inventory_digest",
            "plan_sha256",
            "import_layout_version",
            "target_storage_schema_version",
            "target_schema_sha256",
            "data_generation_id",
            "report_sha256",
            "source_inventory_file_sha256",
            "backup_manifest_sha256",
            "cas_authority_sha256",
            "quarantine_sha256",
            "quarantine_entry_count",
            "completed_at",
            "authority_digest",
        }
        if (
            set(value) != expected_fields
            or value.get("schema_version") != 1
            or value.get("state") != "completed"
            or value.get("source_version") != "0.3.0"
            or value.get("target_version") != "1.0.0"
            or not isinstance(value.get("slot_id"), str)
            or _SAFE_SLOT.fullmatch(value["slot_id"]) is None
            or (
                value.get("transaction_id") is not None
                and (
                    not isinstance(value.get("transaction_id"), str)
                    or _SAFE_TRANSACTION.fullmatch(value["transaction_id"]) is None
                )
            )
            or not isinstance(value.get("migration_id"), str)
            or _MIGRATION_ID.fullmatch(value["migration_id"]) is None
            or any(
                not isinstance(value.get(key), str)
                or _HEX_64.fullmatch(value[key]) is None
                for key in (
                    "source_root_identity_sha256",
                    "source_inventory_digest",
                    "plan_sha256",
                    "target_schema_sha256",
                    "authority_digest",
                )
            )
            or isinstance(value.get("import_layout_version"), bool)
            or not isinstance(value.get("import_layout_version"), int)
            or value["import_layout_version"] < 1
            or isinstance(value.get("target_storage_schema_version"), bool)
            or not isinstance(value.get("target_storage_schema_version"), int)
            or value["target_storage_schema_version"] < 1
            or not isinstance(value.get("data_generation_id"), str)
            or re.fullmatch(r"gen_[0-9a-f]{26}", value["data_generation_id"]) is None
            or isinstance(value.get("quarantine_entry_count"), bool)
            or not isinstance(value.get("quarantine_entry_count"), int)
            or value["quarantine_entry_count"] < 0
            or not isinstance(value.get("completed_at"), str)
            or not value["completed_at"]
            or any(
                not isinstance(value.get(key), str)
                or _HEX_64.fullmatch(value[key]) is None
                for key in _TARGET_AUTHORITY_FIELDS - {"quarantine_sha256"}
            )
            or (
                value["quarantine_entry_count"] > 0
                and (
                    not isinstance(value.get("quarantine_sha256"), str)
                    or _HEX_64.fullmatch(value["quarantine_sha256"]) is None
                )
            )
            or (
                value["quarantine_entry_count"] == 0
                and value.get("quarantine_sha256") is not None
            )
        ):
            raise ProductMigrationError("migration completion authority is invalid")
        try:
            expected_generation = data_generation_id(
                migration_id=value["migration_id"],
                source_inventory_digest=value["source_inventory_digest"],
                import_layout_version=value["import_layout_version"],
                target_storage_schema_version=value["target_storage_schema_version"],
                target_schema_sha256=value["target_schema_sha256"],
            )
        except ImportSchemaIdentityError as error:
            raise ProductMigrationError("migration completion authority is invalid") from error
        if (
            value["data_generation_id"] != expected_generation
            or value["authority_digest"] != _completion_digest(value)
        ):
            raise ProductMigrationError("migration completion authority is inconsistent")
        return value

    @staticmethod
    def _identity_projection(report: Mapping[str, Any]) -> dict[str, Any]:
        schema_identity = ProductLegacyMigrationCoordinator._report_schema_identity(report)
        quarantine = report.get("quarantine")
        authority = _target_authority(report)
        if authority is None:
            raise ProductMigrationError("migrated target file authority is missing")
        return {
            "migration_id": report["migration_id"],
            "source_inventory_digest": report["source_inventory_digest"],
            "import_layout_version": schema_identity.import_layout_version,
            "target_storage_schema_version": (
                schema_identity.target_storage_schema_version
            ),
            "target_schema_sha256": schema_identity.target_schema_sha256,
            "data_generation_id": schema_identity.data_generation_id,
            **authority,
            "quarantine_entry_count": int(
                quarantine.get("entry_count", 0)
                if isinstance(quarantine, Mapping)
                else 0
            ),
        }

    def _assert_receipt_matches(
        self,
        *,
        receipt: Mapping[str, Any] | None,
        plan: ProductMigrationPlan,
        report: Mapping[str, Any],
        slot_id: str,
        allowed_states: set[str],
        transaction_id: str | None,
    ) -> dict[str, Any]:
        if receipt is None or receipt.get("state") not in allowed_states:
            raise ProductMigrationError(
                "migrated Runtime state has no matching activation receipt"
            )
        identity = self._identity_projection(report)
        expected = {
            **identity,
            "source_root_identity_sha256": plan.source_root_identity_sha256,
            "plan_sha256": _plan_sha256(plan),
            "slot_id": slot_id,
        }
        if receipt.get("state") == "publishing":
            for key in _TARGET_AUTHORITY_FIELDS:
                expected.pop(key, None)
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ProductMigrationError(
                "migrated Runtime state differs from its activation receipt"
            )
        if transaction_id is not None and receipt.get("transaction_id") != transaction_id:
            raise ProductMigrationError(
                "migration activation transaction differs from its receipt"
            )
        return dict(receipt)

    def _consume_plan(self, expected_plan_sha256: str) -> None:
        plan = self._load_plan()
        if plan is None:
            return
        if _plan_sha256(plan) != expected_plan_sha256:
            raise ProductMigrationError(
                "migration plan differs from its completion authority"
            )
        try:
            self.plan_path.unlink()
            _fsync_directory(self.migration_root)
        except OSError:
            raise ProductMigrationError("completed migration plan could not be consumed") from None
        self.fault_hook("plan_consumed")

    def _persist_completion(
        self,
        *,
        plan: ProductMigrationPlan,
        report: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = self._identity_projection(report)
        value: dict[str, Any] = {
            "schema_version": 1,
            "state": "completed",
            "source_version": "0.3.0",
            "target_version": "1.0.0",
            "slot_id": receipt["slot_id"],
            "transaction_id": receipt.get("transaction_id"),
            "source_root_identity_sha256": plan.source_root_identity_sha256,
            "plan_sha256": _plan_sha256(plan),
            **identity,
            "completed_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        }
        value["authority_digest"] = _completion_digest(value)
        existing = self._completion()
        if existing is None:
            _atomic_json(self.completion_path, value)
            existing = self._completion()
            self.fault_hook("completion_persisted")
        else:
            immutable_fields = set(value) - {"completed_at", "authority_digest"}
            if any(existing.get(key) != value.get(key) for key in immutable_fields):
                raise ProductMigrationError(
                    "existing migration completion authority is inconsistent"
                )
        assert existing is not None
        self._consume_plan(existing["plan_sha256"])
        return existing

    def _verify_completed(self) -> dict[str, Any] | None:
        completion = self._completion()
        if completion is None:
            return None
        report = self._verify_target(
            self.target_root,
            verify_static_content=False,
            verify_blob_content=False,
            expected_authority=completion,
        )
        receipt = self._receipt()
        if receipt is None or receipt.get("state") != "committed":
            raise ProductMigrationError(
                "migration completion has no committed activation receipt"
            )
        projected = self._identity_projection(report)
        expected = {
            **projected,
            "source_root_identity_sha256": completion["source_root_identity_sha256"],
            "plan_sha256": completion["plan_sha256"],
            "slot_id": completion["slot_id"],
            "transaction_id": completion["transaction_id"],
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ProductMigrationError(
                "migration completion differs from the active data generation"
            )
        if any(completion.get(key) != value for key, value in projected.items()):
            raise ProductMigrationError(
                "migration completion differs from the active data generation"
            )
        self._consume_plan(completion["plan_sha256"])
        return completion

    def completion_authority(self) -> Mapping[str, Any] | None:
        """Return the validated immutable generation barrier for update coordination."""

        completion = self._completion()
        if completion is None:
            return None
        verified = self._verify_completed()
        assert verified is not None
        return dict(verified)

    @staticmethod
    def _report_schema_identity(report: Mapping[str, Any]) -> ImportSchemaIdentity:
        import_layout_version = report.get("import_layout_version")
        storage_schema_version = report.get("storage_schema_version")
        schema_sha256 = report.get("target_schema_sha256")
        generation = report.get("data_generation_id")
        migration_id = report.get("migration_id")
        source_digest = report.get("source_inventory_digest")
        if (
            isinstance(import_layout_version, bool)
            or not isinstance(import_layout_version, int)
            or isinstance(storage_schema_version, bool)
            or not isinstance(storage_schema_version, int)
            or not isinstance(schema_sha256, str)
            or not isinstance(generation, str)
            or not isinstance(migration_id, str)
            or not isinstance(source_digest, str)
        ):
            raise ProductMigrationError("completed migration schema identity is invalid")
        try:
            identity = ImportSchemaIdentity(
                import_layout_version=import_layout_version,
                target_storage_schema_version=storage_schema_version,
                target_schema_sha256=schema_sha256,
                data_generation_id=generation,
            )
            expected = data_generation_id(
                migration_id=migration_id,
                source_inventory_digest=source_digest,
                import_layout_version=identity.import_layout_version,
                target_storage_schema_version=identity.target_storage_schema_version,
                target_schema_sha256=identity.target_schema_sha256,
            )
        except ImportSchemaIdentityError as error:
            raise ProductMigrationError(
                "completed migration schema identity is invalid"
            ) from error
        if identity.data_generation_id != expected:
            raise ProductMigrationError(
                "completed migration data generation identity is inconsistent"
            )
        return identity

    def _load_report(self, target: Path) -> dict[str, Any]:
        report = _regular_json(
            target / REPORT_NAME,
            maximum=_MAX_REPORT_BYTES,
            label="completed migration report",
        )
        digest = report.get("source_inventory_digest")
        migration_id = report.get("migration_id")
        quarantine = report.get("quarantine")
        counts = report.get("counts")
        backups = report.get("backups")
        if (
            _TARGET_AUTHORITY_KEY in report
            or report.get("status") != "completed"
            or report.get("source_version") != "0.3.0"
            or report.get("target_version") != "1.0.0"
            or not isinstance(migration_id, str)
            or _MIGRATION_ID.fullmatch(migration_id) is None
            or not isinstance(digest, str)
            or _HEX_64.fullmatch(digest) is None
            or not isinstance(quarantine, Mapping)
            or isinstance(quarantine.get("entry_count"), bool)
            or not isinstance(quarantine.get("entry_count"), int)
            or quarantine["entry_count"] < 0
            or not isinstance(counts, Mapping)
            or any(
                not isinstance(key, str)
                or not key
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in counts.items()
            )
            or not isinstance(backups, list)
        ):
            raise ProductMigrationError("completed migration report is invalid")
        self._report_schema_identity(report)
        return report

    def _verify_target(
        self,
        target: Path,
        *,
        verify_static_content: bool = True,
        verify_blob_content: bool = True,
        expected_authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            target = secure_directory(
                target,
                label="migrated Runtime state",
                root=self.root,
            )
        except MigrationError:
            raise ProductMigrationError("migrated Runtime state is unavailable") from None
        report = self._load_report(target)
        schema_identity = self._report_schema_identity(report)
        try:
            database = secure_regular_file(
                target / TARGET_DATABASE_NAME,
                label="migrated Runtime database",
                root=target,
            )
            database_before = lstat_identity(
                database,
                label="migrated Runtime database",
            )
            reject_link_or_reparse(
                database_before,
                label="migrated Runtime database",
            )
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                quick = connection.execute("PRAGMA quick_check").fetchall()
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                runs = connection.execute(
                    "SELECT migration_id, source_inventory_digest, status, report_json "
                    "FROM migration_runs"
                ).fetchall()
                digests = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT source_sha256 "
                        "FROM migration_artifact_links ORDER BY source_sha256"
                    ).fetchall()
                )
                memory_digests = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT blob_sha256 "
                        "FROM migration_memory_blob_links ORDER BY blob_sha256"
                    ).fetchall()
                )
                migration_meta = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT key,value FROM migration_meta WHERE key IN ("
                        "'import_layout_version','migration_id',"
                        "'source_inventory_digest','data_generation_id',"
                        "'import_target_storage_schema_version',"
                        "'import_target_schema_sha256') ORDER BY key"
                    ).fetchall()
                }
                storage_row = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
                ).fetchone()
                observed_schema_sha256 = physical_schema_sha256(connection)
            finally:
                connection.close()
            database_after = lstat_identity(
                database,
                label="migrated Runtime database",
            )
            if database_after != database_before:
                raise ProductMigrationError(
                    "migrated Runtime database changed during verification"
                )
        except ProductMigrationError:
            raise
        except (OSError, sqlite3.Error, MigrationError, ImportSchemaIdentityError):
            raise ProductMigrationError("migrated Runtime database failed verification") from None
        stored_report: Mapping[str, Any] | None = None
        if len(runs) == 1:
            try:
                candidate_report = json.loads(str(runs[0][3]))
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate_report = None
            if isinstance(candidate_report, Mapping):
                stored_report = candidate_report
        try:
            observed_storage_version = (
                int(storage_row[0]) if storage_row is not None else 0
            )
        except (TypeError, ValueError, OverflowError):
            observed_storage_version = 0
        expected_meta = {
            "import_layout_version": str(schema_identity.import_layout_version),
            "migration_id": report["migration_id"],
            "source_inventory_digest": report["source_inventory_digest"],
            "data_generation_id": schema_identity.data_generation_id,
            "import_target_storage_schema_version": str(
                schema_identity.target_storage_schema_version
            ),
            "import_target_schema_sha256": schema_identity.target_schema_sha256,
        }
        schema_is_import_generation = (
            observed_storage_version == schema_identity.target_storage_schema_version
            and observed_schema_sha256 == schema_identity.target_schema_sha256
        )
        schema_is_signed_successor = False
        if expected_authority is not None and not schema_is_import_generation:
            try:
                schema_is_signed_successor = (
                    self.storage_schema_authorizer is not None
                    and self.storage_schema_authorizer(
                        observed_storage_version,
                        observed_schema_sha256,
                    )
                    is True
                )
            except Exception:
                schema_is_signed_successor = False
        if (
            [tuple(row) for row in quick] != [("ok",)]
            or foreign_keys
            or migration_meta != expected_meta
            or len(runs) != 1
            or tuple(runs[0][:3])
            != (
                report["migration_id"],
                report["source_inventory_digest"],
                "completed",
            )
            or stored_report is None
            or _canonical_json(stored_report) != _canonical_json(report)
            or not (schema_is_import_generation or schema_is_signed_successor)
        ):
            raise ProductMigrationError("migrated Runtime database is inconsistent")
        all_digests = tuple(dict.fromkeys((*digests, *memory_digests)))
        try:
            authority = verify_target_file_authority(
                target,
                report,
                referenced_digests=all_digests,
                verify_blob_content=verify_blob_content,
                verify_static_content=verify_static_content,
                expected_authority=expected_authority,
            )
        except MigrationError:
            raise ProductMigrationError(
                "migrated Runtime file authority failed verification"
            ) from None
        verified = dict(report)
        verified[_TARGET_AUTHORITY_KEY] = authority.to_dict()
        return verified

    def dry_run(
        self,
        candidate_slot: str | Path,
        transaction_id: str | None = None,
    ) -> bool:
        slot_id = self._candidate_id(candidate_slot)
        if self._verify_completed() is not None:
            return True
        plan = self._load_plan()
        if plan is None:
            return True
        if self.database_path.exists() or (self.target_root / REPORT_NAME).exists():
            report = self._verify_target(self.target_root)
            receipt = self._assert_receipt_matches(
                receipt=self._receipt(),
                plan=plan,
                report=report,
                slot_id=slot_id,
                allowed_states={"swap_pending", "committed"},
                transaction_id=transaction_id,
            )
            self._write_receipt(
                state="committed",
                slot_id=slot_id,
                report=report,
                plan=plan,
                transaction_id=receipt.get("transaction_id"),
            )
            committed = self._receipt()
            assert committed is not None
            self._persist_completion(plan=plan, report=report, receipt=committed)
            return True
        self._validate_plan_source(plan)
        dry_target = self.migration_root / f".dry-run-{slot_id}"
        if os.path.lexists(dry_target):
            raise ProductMigrationError("migration dry-run target is unexpectedly present")
        report = migrate_v030_to_v1(
            plan.source_root,
            dry_target,
            dry_run=True,
            **self._migration_kwargs(plan),
        )
        if report.status != "dry_run_verified":
            raise ProductMigrationError("legacy migration dry-run was not verified")
        self._write_receipt(
            state="dry_run_verified",
            slot_id=slot_id,
            report=report,
            plan=plan,
            transaction_id=transaction_id,
        )
        return True

    def _quarantine_key(self) -> bytes:
        if self.vault is None:
            raise ProductMigrationError("credential vault is required for legacy secrets")
        try:
            stored = self.vault.get(_QUARANTINE_KEY_REFERENCE)
        except (KeyError, RuntimeError):
            candidate = os.urandom(32)
            try:
                self.vault.put(
                    _QUARANTINE_KEY_REFERENCE,
                    {
                        "aes256_gcm_key": base64.urlsafe_b64encode(candidate).decode(
                            "ascii"
                        )
                    },
                )
                stored = self.vault.get(_QUARANTINE_KEY_REFERENCE)
            except (KeyError, RuntimeError):
                raise ProductMigrationError(
                    "credential vault could not persist the migration key"
                ) from None
        try:
            material = base64.b64decode(
                str(stored["aes256_gcm_key"]), altchars=b"-_", validate=True
            )
        except (KeyError, TypeError, ValueError):
            raise ProductMigrationError("credential vault returned an invalid migration key") from None
        if len(material) != 32:
            raise ProductMigrationError("credential vault returned an invalid migration key")
        return material

    def _prior_state_is_replaceable(self) -> None:
        if not os.path.lexists(self.target_root):
            return
        try:
            target = secure_directory(
                self.target_root,
                label="v1 state path",
                root=self.root,
            )
        except MigrationError:
            raise TargetConflictError("v1 state path is not a real directory")
        allowed = {"migration-receipts"}
        observed = {child.name for child in target.iterdir()}
        if not observed.issubset(allowed):
            raise TargetConflictError("v1 state already contains non-migration data")
        for child in target.iterdir():
            try:
                secure_directory(
                    child,
                    label="v1 preflight entry",
                    root=target,
                )
            except MigrationError:
                raise TargetConflictError("v1 state contains an unsafe preflight entry")

    def _activate_prepared(
        self,
        *,
        plan: ProductMigrationPlan,
        slot_id: str,
        transaction_id: str | None,
        prepared_report: Mapping[str, Any],
    ) -> dict[str, Any]:
        prepared_authority = _target_authority(prepared_report)
        if prepared_authority is None:
            raise ProductMigrationError("prepared migration authority is unavailable")
        report = self._verify_target(
            self.prepared_root,
            verify_static_content=False,
            verify_blob_content=False,
            expected_authority=prepared_authority,
        )
        backup = self.migration_root / f"v030-prior-state-{report['migration_id']}"
        if os.path.lexists(self.target_root) and (self.target_root / REPORT_NAME).exists():
            live = self._verify_target(self.target_root)
            if self._identity_projection(live) != self._identity_projection(report):
                raise TargetConflictError("live v1 state belongs to another legacy source")
            receipt = self._assert_receipt_matches(
                receipt=self._receipt(),
                plan=plan,
                report=live,
                slot_id=slot_id,
                allowed_states={"swap_pending", "committed"},
                transaction_id=transaction_id,
            )
            self._write_receipt(
                state="committed",
                slot_id=slot_id,
                report=live,
                plan=plan,
                transaction_id=receipt.get("transaction_id"),
            )
            return live
        self._prior_state_is_replaceable()
        self._write_receipt(
            state="swap_pending",
            slot_id=slot_id,
            report=report,
            plan=plan,
            transaction_id=transaction_id,
        )
        if os.path.lexists(self.target_root):
            if os.path.lexists(backup):
                raise ProductMigrationError("migration prior-state backup is ambiguous")
            os.replace(self.target_root, backup)
            _fsync_directory(self.root)
            _fsync_directory(self.migration_root)
            self.fault_hook("prior_state_renamed")
        if not os.path.lexists(self.target_root):
            if not os.path.lexists(self.prepared_root):
                raise ProductMigrationError("prepared migration state disappeared during activation")
            os.replace(self.prepared_root, self.target_root)
            _fsync_directory(self.root)
            _fsync_directory(self.migration_root)
            self.fault_hook("migrated_state_activated")
        live = self._verify_target(
            self.target_root,
            verify_static_content=False,
            verify_blob_content=False,
            expected_authority=prepared_authority,
        )
        if self._identity_projection(live) != self._identity_projection(report):
            raise ProductMigrationError("activated migration state changed during swap")
        self._write_receipt(
            state="committed",
            slot_id=slot_id,
            report=live,
            plan=plan,
            transaction_id=transaction_id,
        )
        return live

    def commit(
        self,
        candidate_slot: str | Path,
        transaction_id: str | None = None,
    ) -> bool:
        slot_id = self._candidate_id(candidate_slot)
        if self._verify_completed() is not None:
            return True
        plan = self._load_plan()
        if plan is None:
            return True
        if self.database_path.exists() or (self.target_root / REPORT_NAME).exists():
            report = self._verify_target(self.target_root)
            receipt = self._assert_receipt_matches(
                receipt=self._receipt(),
                plan=plan,
                report=report,
                slot_id=slot_id,
                allowed_states={"swap_pending", "committed"},
                transaction_id=transaction_id,
            )
            self._write_receipt(
                state="committed",
                slot_id=slot_id,
                report=report,
                plan=plan,
                transaction_id=receipt.get("transaction_id"),
            )
            committed = self._receipt()
            assert committed is not None
            self._persist_completion(plan=plan, report=report, receipt=committed)
            return True
        receipt = self._receipt()
        if (
            receipt is None
            or receipt["slot_id"] != slot_id
            or receipt["plan_sha256"] != _plan_sha256(plan)
            or receipt["source_root_identity_sha256"]
            != plan.source_root_identity_sha256
            or receipt["state"]
            not in {"dry_run_verified", "publishing", "prepared", "swap_pending"}
        ):
            self._validate_plan_source(plan)
            self.dry_run(candidate_slot, transaction_id)
            receipt = self._receipt()
        assert receipt is not None
        effective_transaction_id = (
            transaction_id
            if transaction_id is not None
            else receipt.get("transaction_id")
        )
        if not os.path.lexists(self.prepared_root):
            self._validate_plan_source(plan)
            quarantine_key = (
                self._quarantine_key()
                if int(receipt["quarantine_entry_count"]) > 0
                else None
            )
            # Persist the exact dry-run identity before the standalone migrator
            # publishes ``prepared_root``.  A process death after its atomic
            # rename but before the prepared receipt can then reconcile only an
            # exact report/meta/CAS target instead of becoming unrecoverable.
            if receipt["state"] != "publishing":
                self._write_receipt(
                    state="publishing",
                    slot_id=slot_id,
                    report=receipt,
                    plan=plan,
                    transaction_id=effective_transaction_id,
                )
                receipt = self._receipt()
                assert receipt is not None
            migrated = migrate_v030_to_v1(
                plan.source_root,
                self.prepared_root,
                quarantine_key=quarantine_key,
                **self._migration_kwargs(plan),
            )
            if (
                migrated.status != "completed"
                or migrated.source_inventory_digest != receipt["source_inventory_digest"]
            ):
                raise ProductMigrationError("committed migration differs from its dry-run")
            report = self._verify_target(self.prepared_root)
            self._write_receipt(
                state="prepared",
                slot_id=slot_id,
                report=report,
                plan=plan,
                transaction_id=effective_transaction_id,
            )
            self.fault_hook("migration_prepared")
        else:
            report = self._verify_target(self.prepared_root)
            self._assert_receipt_matches(
                receipt=receipt,
                plan=plan,
                report=report,
                slot_id=slot_id,
                allowed_states={"publishing", "prepared", "swap_pending"},
                transaction_id=effective_transaction_id,
            )
            if receipt["state"] == "publishing":
                self._write_receipt(
                    state="prepared",
                    slot_id=slot_id,
                    report=report,
                    plan=plan,
                    transaction_id=effective_transaction_id,
                )
                receipt = self._receipt()
                assert receipt is not None
        live = self._activate_prepared(
            plan=plan,
            slot_id=slot_id,
            transaction_id=effective_transaction_id,
            prepared_report=report,
        )
        committed = self._receipt()
        assert committed is not None
        self._persist_completion(plan=plan, report=live, receipt=committed)
        return True

    def cleanup_prior_state(self) -> bool:
        """Remove the empty pre-v1 state only after the v1 data barrier is durable."""

        completion = self._completion()
        if completion is None:
            return False
        self._verify_completed()
        backup = self.migration_root / f"v030-prior-state-{completion['migration_id']}"
        if not os.path.lexists(backup):
            return False
        self._prior_backup_is_safe(backup)
        shutil.rmtree(backup)
        _fsync_directory(self.migration_root)
        return True

    @staticmethod
    def _prior_backup_is_safe(backup: Path) -> None:
        try:
            backup = secure_directory(backup, label="migration prior-state backup")
        except MigrationError:
            raise ProductMigrationError("migration prior-state backup is unsafe")
        observed = {child.name for child in backup.iterdir()}
        if not observed.issubset({"migration-receipts"}):
            raise ProductMigrationError("migration prior-state backup contains user data")

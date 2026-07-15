"""Bounded declarative storage migrations for signed product Runtime cores.

The admission Runtime never imports or executes Python from a candidate slot.
It only parses this closed JSON grammar and renders a small allowlist of SQLite
DDL operations itself.  The exact same parsed plan is used for copy-on-write
admission and, after the activation data barrier, the live database.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Mapping


STORAGE_MIGRATION_FILE_NAME = "storage-migrations.json"
STORAGE_MIGRATION_DOCUMENT_TYPE = "ecorex.storage-migration-plan"
STORAGE_MIGRATION_RECEIPT_TYPE = "ecorex.storage-migration-receipt"
STORAGE_MIGRATION_SCHEMA_VERSION = 2
_LEGACY_STORAGE_MIGRATION_SCHEMA_VERSION = 1
_STORAGE_MIGRATION_RECEIPT_SCHEMA_VERSION = 1
MAX_STORAGE_MIGRATION_BYTES = 64 * 1024
MAX_MIGRATION_STEPS = 16
MAX_OPERATIONS_PER_STEP = 64
MAX_TABLES_IN_RECEIPT = 4096
_SAFE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_AFFINITIES = frozenset({"INTEGER", "REAL", "TEXT", "BLOB"})
_PHASES = frozenset({"admission_dry_run", "live_preflight", "live"})
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_EMPTY_SCHEMA_SHA256 = hashlib.sha256(b"[]").hexdigest()


class StorageMigrationError(RuntimeError):
    """A migration plan, receipt, snapshot, or live application is invalid."""


@dataclass(frozen=True, slots=True)
class StorageMigrationOperation:
    """One normalized operation in the closed declarative grammar."""

    payload: bytes

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self.payload)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class StorageMigrationStep:
    step_id: str
    from_schema_version: int
    to_schema_version: int
    operations: tuple[StorageMigrationOperation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "from_schema_version": self.from_schema_version,
            "to_schema_version": self.to_schema_version,
            "operations": [operation.to_dict() for operation in self.operations],
        }


@dataclass(frozen=True, slots=True)
class StorageMigrationManifest:
    schema_version: int
    document_type: str
    target_schema_version: int
    target_schema_sha256: str | None
    steps: tuple[StorageMigrationStep, ...]

    def __post_init__(self) -> None:
        if self.schema_version == STORAGE_MIGRATION_SCHEMA_VERSION:
            if (
                not isinstance(self.target_schema_sha256, str)
                or _HEX_256.fullmatch(self.target_schema_sha256) is None
            ):
                raise StorageMigrationError(
                    "storage migration target schema digest is invalid"
                )
        elif self.schema_version == _LEGACY_STORAGE_MIGRATION_SCHEMA_VERSION:
            if self.target_schema_sha256 is not None:
                raise StorageMigrationError(
                    "legacy storage migration cannot declare a target schema digest"
                )
        else:
            raise StorageMigrationError("storage migration manifest schema is unsupported")

    @classmethod
    def current(
        cls,
        target_schema_version: int,
        *,
        target_schema_sha256: str | None = None,
    ) -> "StorageMigrationManifest":
        if not _schema_number(target_schema_version):
            raise StorageMigrationError("target storage schema version is invalid")
        if target_schema_sha256 is None:
            from .database import SCHEMA_VERSION

            if target_schema_version != SCHEMA_VERSION:
                raise StorageMigrationError(
                    "non-current storage target requires an explicit schema digest"
                )
            target_schema_sha256 = current_storage_schema_sha256()
        return cls(
            schema_version=STORAGE_MIGRATION_SCHEMA_VERSION,
            document_type=STORAGE_MIGRATION_DOCUMENT_TYPE,
            target_schema_version=target_schema_version,
            target_schema_sha256=target_schema_sha256,
            steps=(),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "StorageMigrationManifest":
        """Parse the current product contract; legacy plans fail closed."""

        return cls._from_bytes(payload, allow_legacy_v1=False)

    @classmethod
    def from_legacy_v1_bytes_for_test(
        cls, payload: bytes
    ) -> "StorageMigrationManifest":
        """Parse v1 only at an explicit non-product compatibility boundary."""

        manifest = cls._from_bytes(payload, allow_legacy_v1=True)
        if manifest.schema_version != _LEGACY_STORAGE_MIGRATION_SCHEMA_VERSION:
            raise StorageMigrationError(
                "legacy storage migration test boundary requires a v1 manifest"
            )
        return manifest

    @classmethod
    def _from_bytes(
        cls, payload: bytes, *, allow_legacy_v1: bool
    ) -> "StorageMigrationManifest":
        raw = _load_canonical_json(payload, label="storage migration manifest")
        manifest_schema = raw.get("schema_version")
        if manifest_schema == STORAGE_MIGRATION_SCHEMA_VERSION:
            _exact_keys(
                raw,
                {
                    "schema_version",
                    "document_type",
                    "target_schema_version",
                    "target_schema_sha256",
                    "steps",
                },
                "storage migration manifest",
            )
            target_schema_sha256 = raw["target_schema_sha256"]
            if (
                not isinstance(target_schema_sha256, str)
                or _HEX_256.fullmatch(target_schema_sha256) is None
            ):
                raise StorageMigrationError(
                    "storage migration target schema digest is invalid"
                )
        elif manifest_schema == _LEGACY_STORAGE_MIGRATION_SCHEMA_VERSION:
            if not allow_legacy_v1:
                raise StorageMigrationError(
                    "legacy storage migration manifest requires an explicit test boundary"
                )
            _exact_keys(
                raw,
                {"schema_version", "document_type", "target_schema_version", "steps"},
                "storage migration manifest",
            )
            target_schema_sha256 = None
        else:
            raise StorageMigrationError("storage migration manifest schema is unsupported")
        if raw["document_type"] != STORAGE_MIGRATION_DOCUMENT_TYPE:
            raise StorageMigrationError("storage migration manifest type is invalid")
        target = raw["target_schema_version"]
        if not _schema_number(target):
            raise StorageMigrationError("target storage schema version is invalid")
        raw_steps = raw["steps"]
        if not isinstance(raw_steps, list) or len(raw_steps) > MAX_MIGRATION_STEPS:
            raise StorageMigrationError("storage migration steps are not bounded")
        steps: list[StorageMigrationStep] = []
        step_ids: set[str] = set()
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                raise StorageMigrationError("storage migration step must be an object")
            _exact_keys(
                raw_step,
                {
                    "step_id",
                    "from_schema_version",
                    "to_schema_version",
                    "operations",
                },
                "storage migration step",
            )
            step_id = raw_step["step_id"]
            source = raw_step["from_schema_version"]
            destination = raw_step["to_schema_version"]
            raw_operations = raw_step["operations"]
            if not isinstance(step_id, str) or _SAFE_ID.fullmatch(step_id) is None:
                raise StorageMigrationError("storage migration step ID is unsafe")
            if step_id in step_ids:
                raise StorageMigrationError("storage migration step IDs must be unique")
            if (
                not _schema_number(source)
                or not _schema_number(destination)
                or destination != source + 1
            ):
                raise StorageMigrationError(
                    "storage migration steps must advance exactly one schema version"
                )
            if (
                not isinstance(raw_operations, list)
                or not raw_operations
                or len(raw_operations) > MAX_OPERATIONS_PER_STEP
            ):
                raise StorageMigrationError(
                    "storage migration operations must be a non-empty bounded array"
                )
            operations = tuple(
                StorageMigrationOperation(
                    _canonical_json_bytes(_normalize_operation(operation))
                )
                for operation in raw_operations
            )
            steps.append(
                StorageMigrationStep(step_id, source, destination, operations)
            )
            step_ids.add(step_id)
        for previous, following in zip(steps, steps[1:]):
            if previous.to_schema_version != following.from_schema_version:
                raise StorageMigrationError("storage migration steps must be contiguous")
        if steps and steps[-1].to_schema_version != target:
            raise StorageMigrationError(
                "storage migration steps do not reach the target schema"
            )
        manifest = cls(
            schema_version=manifest_schema,
            document_type=STORAGE_MIGRATION_DOCUMENT_TYPE,
            target_schema_version=target,
            target_schema_sha256=target_schema_sha256,
            steps=tuple(steps),
        )
        if manifest.to_bytes() != payload:
            raise StorageMigrationError(
                "storage migration manifest must use canonical JSON"
            )
        return manifest

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "document_type": self.document_type,
            "target_schema_version": self.target_schema_version,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.schema_version == STORAGE_MIGRATION_SCHEMA_VERSION:
            value["target_schema_sha256"] = self.target_schema_sha256
        return value

    def to_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def path_from(self, source_schema_version: int) -> tuple[StorageMigrationStep, ...]:
        if not _schema_number(source_schema_version):
            raise StorageMigrationError("source storage schema version is invalid")
        if source_schema_version == self.target_schema_version:
            return ()
        by_source = {step.from_schema_version: step for step in self.steps}
        selected: list[StorageMigrationStep] = []
        current = source_schema_version
        while current != self.target_schema_version:
            step = by_source.get(current)
            if step is None or step.to_schema_version > self.target_schema_version:
                raise StorageMigrationError(
                    "storage migration manifest has no path from the source schema"
                )
            selected.append(step)
            current = step.to_schema_version
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class StorageMigrationIdentity:
    release_id: str
    build_digest: str
    artifact_id: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or _SAFE_ID.fullmatch(self.release_id) is None:
            raise StorageMigrationError("migration release ID is unsafe")
        if not isinstance(self.artifact_id, str) or _SAFE_ID.fullmatch(self.artifact_id) is None:
            raise StorageMigrationError("migration artifact ID is unsafe")
        for value in (self.build_digest, self.artifact_sha256):
            if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
                raise StorageMigrationError("migration release digest is invalid")


@dataclass(frozen=True, slots=True)
class StorageMigrationReceipt:
    phase: str
    identity: StorageMigrationIdentity
    plan_sha256: str
    source_schema_version: int
    target_schema_version: int
    source_database_sha256: str
    target_database_sha256: str
    source_schema_sha256: str
    target_schema_sha256: str
    source_table_counts: Mapping[str, int]
    target_table_counts: Mapping[str, int]
    quick_check: str
    foreign_key_violations: int
    created_at: str
    receipt_digest: str

    def __post_init__(self) -> None:
        for value in (self.source_schema_sha256, self.target_schema_sha256):
            if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
                raise StorageMigrationError(
                    "storage migration receipt schema digest is invalid"
                )

    @classmethod
    def create(
        cls,
        *,
        phase: str,
        identity: StorageMigrationIdentity,
        manifest: StorageMigrationManifest,
        source_schema_version: int,
        source_database_sha256: str,
        target_database_sha256: str,
        source_schema_sha256: str,
        target_schema_sha256: str,
        source_table_counts: Mapping[str, int],
        target_table_counts: Mapping[str, int],
        quick_check: str,
        foreign_key_violations: int,
    ) -> "StorageMigrationReceipt":
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        unsigned = _receipt_dict(
            phase=phase,
            identity=identity,
            plan_sha256=manifest.sha256,
            source_schema_version=source_schema_version,
            target_schema_version=manifest.target_schema_version,
            source_database_sha256=source_database_sha256,
            target_database_sha256=target_database_sha256,
            source_schema_sha256=source_schema_sha256,
            target_schema_sha256=target_schema_sha256,
            source_table_counts=source_table_counts,
            target_table_counts=target_table_counts,
            quick_check=quick_check,
            foreign_key_violations=foreign_key_violations,
            created_at=created_at,
        )
        return cls(
            phase=phase,
            identity=identity,
            plan_sha256=manifest.sha256,
            source_schema_version=source_schema_version,
            target_schema_version=manifest.target_schema_version,
            source_database_sha256=source_database_sha256,
            target_database_sha256=target_database_sha256,
            source_schema_sha256=source_schema_sha256,
            target_schema_sha256=target_schema_sha256,
            source_table_counts=MappingProxyType(dict(source_table_counts)),
            target_table_counts=MappingProxyType(dict(target_table_counts)),
            quick_check=quick_check,
            foreign_key_violations=foreign_key_violations,
            created_at=created_at,
            receipt_digest=_receipt_digest(unsigned),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "StorageMigrationReceipt":
        raw = _load_canonical_json(payload, label="storage migration receipt")
        expected = {
            "schema_version",
            "document_type",
            "phase",
            "release_id",
            "build_digest",
            "artifact_id",
            "artifact_sha256",
            "plan_sha256",
            "source_schema_version",
            "target_schema_version",
            "source_database_sha256",
            "target_database_sha256",
            "source_schema_sha256",
            "target_schema_sha256",
            "source_table_counts",
            "target_table_counts",
            "quick_check",
            "foreign_key_violations",
            "created_at",
            "receipt_digest",
        }
        _exact_keys(raw, expected, "storage migration receipt")
        if (
            raw["schema_version"] != _STORAGE_MIGRATION_RECEIPT_SCHEMA_VERSION
            or raw["document_type"] != STORAGE_MIGRATION_RECEIPT_TYPE
        ):
            raise StorageMigrationError("storage migration receipt type is invalid")
        identity = StorageMigrationIdentity(
            release_id=raw["release_id"],
            build_digest=raw["build_digest"],
            artifact_id=raw["artifact_id"],
            artifact_sha256=raw["artifact_sha256"],
        )
        phase = raw["phase"]
        plan_sha256 = raw["plan_sha256"]
        source_sha = raw["source_database_sha256"]
        target_sha = raw["target_database_sha256"]
        source_schema_sha = raw["source_schema_sha256"]
        target_schema_sha = raw["target_schema_sha256"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise StorageMigrationError("storage migration receipt phase is invalid")
        if any(
            not isinstance(value, str) or _HEX_256.fullmatch(value) is None
            for value in (
                plan_sha256,
                source_sha,
                target_sha,
                source_schema_sha,
                target_schema_sha,
                raw["receipt_digest"],
            )
        ):
            raise StorageMigrationError("storage migration receipt digest is invalid")
        source_version = raw["source_schema_version"]
        target_version = raw["target_schema_version"]
        if not _schema_number(source_version) or not _schema_number(target_version):
            raise StorageMigrationError("storage migration receipt schema is invalid")
        source_counts = _validate_table_counts(raw["source_table_counts"])
        target_counts = _validate_table_counts(raw["target_table_counts"])
        quick_check = raw["quick_check"]
        violations = raw["foreign_key_violations"]
        created_at = raw["created_at"]
        if quick_check != "ok" or violations != 0:
            raise StorageMigrationError("storage migration receipt did not pass integrity checks")
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise StorageMigrationError("storage migration receipt time is invalid")
        unsigned = dict(raw)
        observed_digest = unsigned.pop("receipt_digest")
        if not _constant_time_equal(_receipt_digest(unsigned), observed_digest):
            raise StorageMigrationError("storage migration receipt checksum is invalid")
        receipt = cls(
            phase=phase,
            identity=identity,
            plan_sha256=plan_sha256,
            source_schema_version=source_version,
            target_schema_version=target_version,
            source_database_sha256=source_sha,
            target_database_sha256=target_sha,
            source_schema_sha256=source_schema_sha,
            target_schema_sha256=target_schema_sha,
            source_table_counts=MappingProxyType(source_counts),
            target_table_counts=MappingProxyType(target_counts),
            quick_check=quick_check,
            foreign_key_violations=violations,
            created_at=created_at,
            receipt_digest=observed_digest,
        )
        if receipt.to_bytes() != payload:
            raise StorageMigrationError("storage migration receipt must use canonical JSON")
        return receipt

    def to_dict(self) -> dict[str, Any]:
        value = _receipt_dict(
            phase=self.phase,
            identity=self.identity,
            plan_sha256=self.plan_sha256,
            source_schema_version=self.source_schema_version,
            target_schema_version=self.target_schema_version,
            source_database_sha256=self.source_database_sha256,
            target_database_sha256=self.target_database_sha256,
            source_schema_sha256=self.source_schema_sha256,
            target_schema_sha256=self.target_schema_sha256,
            source_table_counts=self.source_table_counts,
            target_table_counts=self.target_table_counts,
            quick_check=self.quick_check,
            foreign_key_violations=self.foreign_key_violations,
            created_at=self.created_at,
        )
        value["receipt_digest"] = self.receipt_digest
        return value

    def to_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_dict())

    def matches(
        self,
        *,
        identity: StorageMigrationIdentity,
        manifest: StorageMigrationManifest,
        phase: str | None = None,
        source_schema_sha256: str | None = None,
        target_schema_sha256: str | None = None,
    ) -> bool:
        return (
            self.identity == identity
            and self.plan_sha256 == manifest.sha256
            and self.target_schema_version == manifest.target_schema_version
            and (
                manifest.target_schema_sha256 is None
                or _constant_time_equal(
                    self.target_schema_sha256,
                    manifest.target_schema_sha256,
                )
            )
            and (phase is None or self.phase == phase)
            and (
                source_schema_sha256 is None
                or _constant_time_equal(
                    self.source_schema_sha256, source_schema_sha256
                )
            )
            and (
                target_schema_sha256 is None
                or _constant_time_equal(
                    self.target_schema_sha256, target_schema_sha256
                )
            )
        )


def migration_receipt_path(
    receipt_root: Path,
    identity: StorageMigrationIdentity,
    phase: str,
) -> Path:
    if phase not in _PHASES:
        raise StorageMigrationError("storage migration receipt phase is invalid")
    name = hashlib.sha256(
        (
            "ecorex-storage-migration-receipt-v1\0"
            + identity.release_id
            + "\0"
            + identity.build_digest
            + "\0"
            + identity.artifact_id
            + "\0"
            + identity.artifact_sha256
            + "\0"
            + phase
        ).encode("utf-8")
    ).hexdigest()
    return receipt_root / f"{name}.json"


def _require_bound_target_manifest(manifest: StorageMigrationManifest) -> None:
    if not isinstance(manifest, StorageMigrationManifest):
        raise StorageMigrationError("storage migration manifest is invalid")
    if (
        manifest.schema_version != STORAGE_MIGRATION_SCHEMA_VERSION
        or manifest.target_schema_sha256 is None
    ):
        raise StorageMigrationError(
            "storage migration activation requires a signed target schema digest"
        )


def dry_run_storage_migration(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
    phase: str = "admission_dry_run",
) -> StorageMigrationReceipt:
    """Apply a plan to a consistent SQLite backup and persist its receipt."""

    try:
        return _dry_run_storage_migration(
            database_path,
            manifest=manifest,
            identity=identity,
            receipt_root=receipt_root,
            phase=phase,
        )
    except StorageMigrationError:
        raise
    except (OSError, sqlite3.Error):
        # Native SQLite and filesystem errors can contain installation paths or
        # provider details.  Keep the public migration boundary typed and
        # redacted so server startup can preserve the pre-data/roll-forward
        # distinction without leaking those values through the process CLI.
        raise StorageMigrationError(
            "copy-on-write storage migration failed"
        ) from None


def _dry_run_storage_migration(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
    phase: str,
) -> StorageMigrationReceipt:

    _require_bound_target_manifest(manifest)
    if phase not in {"admission_dry_run", "live_preflight"}:
        raise StorageMigrationError("copy-on-write migration phase is invalid")
    database = Path(database_path).resolve()
    _validate_database_source(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ecorex-storage-migration-",
        suffix=".sqlite3",
        dir=database.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _copy_database_snapshot(database, temporary)
        source_schema, source_counts, source_schema_sha = _inspect_database(
            temporary, new_database_schema=manifest.target_schema_version
        )
        source_sha = _sha256_file(temporary)
        if source_counts:
            _apply_plan_file(temporary, manifest, source_schema)
        else:
            _bootstrap_new_product_database(temporary, manifest)
        (
            target_schema,
            target_counts,
            target_schema_sha,
            quick_check,
            violations,
        ) = _inspect_integrity(
            temporary, new_database_schema=manifest.target_schema_version
        )
        if target_schema != manifest.target_schema_version:
            raise StorageMigrationError("copy-on-write migration reached the wrong schema")
        if target_schema_sha != manifest.target_schema_sha256:
            raise StorageMigrationError(
                "copy-on-write migration target schema does not match the signed manifest"
            )
        target_sha = _sha256_file(temporary)
        receipt = StorageMigrationReceipt.create(
            phase=phase,
            identity=identity,
            manifest=manifest,
            source_schema_version=source_schema,
            source_database_sha256=source_sha,
            target_database_sha256=target_sha,
            source_schema_sha256=source_schema_sha,
            target_schema_sha256=target_schema_sha,
            source_table_counts=source_counts,
            target_table_counts=target_counts,
            quick_check=quick_check,
            foreign_key_violations=violations,
        )
        _write_receipt(
            migration_receipt_path(receipt_root, identity, phase), receipt
        )
        return receipt
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_live_storage_migration(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
    preflight: StorageMigrationReceipt,
) -> StorageMigrationReceipt:
    """Apply the preflighted plan to live storage after the data barrier."""

    try:
        return _apply_live_storage_migration(
            database_path,
            manifest=manifest,
            identity=identity,
            receipt_root=receipt_root,
            preflight=preflight,
        )
    except StorageMigrationError:
        raise
    except (OSError, sqlite3.Error):
        # At this boundary the caller has already committed the activation data
        # barrier.  Returning the one typed failure is what makes Bootstrap
        # retain the signed candidate and demand a roll-forward repair.
        raise StorageMigrationError("live storage migration failed") from None


def _apply_live_storage_migration(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
    preflight: StorageMigrationReceipt,
) -> StorageMigrationReceipt:

    _require_bound_target_manifest(manifest)
    if not preflight.matches(
        identity=identity, manifest=manifest, phase="live_preflight"
    ):
        raise StorageMigrationError("live migration preflight identity is invalid")
    database = Path(database_path).resolve()
    _validate_database_source(database)
    descriptor, snapshot_name = tempfile.mkstemp(
        prefix=".ecorex-live-migration-source-",
        suffix=".sqlite3",
        dir=database.parent,
    )
    os.close(descriptor)
    snapshot = Path(snapshot_name)
    try:
        _copy_database_snapshot(database, snapshot)
        source_schema, source_counts, source_schema_sha = _inspect_database(
            snapshot, new_database_schema=manifest.target_schema_version
        )
        source_sha = _sha256_file(snapshot)
    finally:
        try:
            snapshot.unlink()
        except FileNotFoundError:
            pass
    if (
        source_schema != preflight.source_schema_version
        or source_sha != preflight.source_database_sha256
        or not preflight.matches(
            identity=identity,
            manifest=manifest,
            phase="live_preflight",
            source_schema_sha256=source_schema_sha,
        )
        or dict(source_counts) != dict(preflight.source_table_counts)
    ):
        raise StorageMigrationError(
            "live storage changed after its copy-on-write migration preflight"
        )

    if source_counts:
        _apply_plan_file(database, manifest, source_schema)
    else:
        _bootstrap_new_product_database(database, manifest)

    descriptor, target_name = tempfile.mkstemp(
        prefix=".ecorex-live-migration-target-",
        suffix=".sqlite3",
        dir=database.parent,
    )
    os.close(descriptor)
    target = Path(target_name)
    try:
        _copy_database_snapshot(database, target)
        (
            target_schema,
            target_counts,
            target_schema_sha,
            quick_check,
            violations,
        ) = _inspect_integrity(
            target, new_database_schema=manifest.target_schema_version
        )
        target_sha = _sha256_file(target)
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    if target_schema != manifest.target_schema_version:
        raise StorageMigrationError("live migration reached the wrong schema")
    if target_schema_sha != manifest.target_schema_sha256:
        raise StorageMigrationError(
            "live migration target schema does not match the signed manifest"
        )
    if not preflight.matches(
        identity=identity,
        manifest=manifest,
        phase="live_preflight",
        target_schema_sha256=target_schema_sha,
    ):
        raise StorageMigrationError(
            "live migration target schema does not match its copy-on-write preflight"
        )
    receipt = StorageMigrationReceipt.create(
        phase="live",
        identity=identity,
        manifest=manifest,
        source_schema_version=source_schema,
        source_database_sha256=source_sha,
        target_database_sha256=target_sha,
        source_schema_sha256=source_schema_sha,
        target_schema_sha256=target_schema_sha,
        source_table_counts=source_counts,
        target_table_counts=target_counts,
        quick_check=quick_check,
        foreign_key_violations=violations,
    )
    _write_receipt(migration_receipt_path(receipt_root, identity, "live"), receipt)
    return receipt


def load_live_storage_migration_receipt(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
) -> StorageMigrationReceipt | None:
    """Return a verified receipt when this signed plan already reached live DB."""

    try:
        return _load_live_storage_migration_receipt(
            database_path,
            manifest=manifest,
            identity=identity,
            receipt_root=receipt_root,
        )
    except StorageMigrationError:
        raise
    except (OSError, sqlite3.Error):
        raise StorageMigrationError(
            "live storage migration receipt verification failed"
        ) from None


def _load_live_storage_migration_receipt(
    database_path: Path,
    *,
    manifest: StorageMigrationManifest,
    identity: StorageMigrationIdentity,
    receipt_root: Path,
) -> StorageMigrationReceipt | None:

    _require_bound_target_manifest(manifest)
    _validate_receipt_directory(receipt_root, create=False)
    path = migration_receipt_path(receipt_root, identity, "live")
    if not path.exists():
        return None
    receipt = StorageMigrationReceipt.from_bytes(_read_regular_file(path))
    if not receipt.matches(identity=identity, manifest=manifest, phase="live"):
        raise StorageMigrationError("live migration receipt does not match the candidate")
    database = Path(database_path).resolve()
    _validate_database_source(database)
    observed_schema_sha = _EMPTY_SCHEMA_SHA256
    if database.exists() and database.stat().st_size:
        (
            observed,
            _counts,
            observed_schema_sha,
            _quick_check,
            _violations,
        ) = _inspect_integrity(
            database, new_database_schema=manifest.target_schema_version
        )
        if observed != manifest.target_schema_version:
            raise StorageMigrationError("live database no longer matches its migration receipt")
    if not receipt.matches(
        identity=identity,
        manifest=manifest,
        phase="live",
        target_schema_sha256=observed_schema_sha,
    ):
        raise StorageMigrationError(
            "live database schema no longer matches its migration receipt"
        )
    return receipt


def _apply_plan_file(
    database_path: Path,
    manifest: StorageMigrationManifest,
    source_schema_version: int,
) -> None:
    selected = manifest.path_from(source_schema_version)
    if not selected:
        return
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        for step in selected:
            for operation in step.operations:
                _execute_operation(connection, operation.to_dict())
            cursor = connection.execute(
                "UPDATE runtime_meta SET value = ? "
                "WHERE key = 'storage_schema_version'",
                (str(step.to_schema_version),),
            )
            if cursor.rowcount != 1:
                raise StorageMigrationError(
                    "live storage has no authoritative schema version record"
                )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _execute_operation(connection: sqlite3.Connection, operation: Mapping[str, Any]) -> None:
    kind = operation["op"]
    if kind == "create_table":
        columns = ", ".join(_column_sql(column) for column in operation["columns"])
        connection.execute(
            f"CREATE TABLE {_quote(operation['table'])} ({columns})"
        )
        return
    if kind == "add_column":
        connection.execute(
            f"ALTER TABLE {_quote(operation['table'])} ADD COLUMN "
            + _column_sql(operation["column"], allow_primary_key=False)
        )
        return
    if kind == "create_index":
        unique = "UNIQUE " if operation["unique"] else ""
        columns = ", ".join(_quote(value) for value in operation["columns"])
        connection.execute(
            f"CREATE {unique}INDEX {_quote(operation['name'])} "
            f"ON {_quote(operation['table'])} ({columns})"
        )
        return
    if kind == "rename_table":
        connection.execute(
            f"ALTER TABLE {_quote(operation['from'])} "
            f"RENAME TO {_quote(operation['to'])}"
        )
        return
    if kind == "rename_column":
        connection.execute(
            f"ALTER TABLE {_quote(operation['table'])} "
            f"RENAME COLUMN {_quote(operation['from'])} TO {_quote(operation['to'])}"
        )
        return
    if kind == "drop_index":
        connection.execute(f"DROP INDEX {_quote(operation['name'])}")
        return
    raise StorageMigrationError("storage migration operation is unsupported")


def _normalize_operation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageMigrationError("storage migration operation must be an object")
    kind = value.get("op")
    if kind == "create_table":
        _exact_keys(value, {"op", "table", "columns"}, "create-table operation")
        table = _identifier(value["table"], "table")
        raw_columns = value["columns"]
        if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= 64:
            raise StorageMigrationError("create-table columns are not bounded")
        columns = [_normalize_column(column) for column in raw_columns]
        names = [column["name"] for column in columns]
        if len(names) != len(set(names)):
            raise StorageMigrationError("create-table column names must be unique")
        if sum(bool(column.get("primary_key")) for column in columns) > 1:
            raise StorageMigrationError("create-table supports one primary key column")
        return {"op": kind, "table": table, "columns": columns}
    if kind == "add_column":
        _exact_keys(value, {"op", "table", "column"}, "add-column operation")
        column = _normalize_column(value["column"], allow_primary_key=False)
        return {
            "op": kind,
            "table": _identifier(value["table"], "table"),
            "column": column,
        }
    if kind == "create_index":
        _exact_keys(
            value, {"op", "name", "table", "columns", "unique"}, "create-index operation"
        )
        raw_columns = value["columns"]
        if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= 16:
            raise StorageMigrationError("create-index columns are not bounded")
        columns = [_identifier(column, "index column") for column in raw_columns]
        if len(columns) != len(set(columns)):
            raise StorageMigrationError("create-index columns must be unique")
        if not isinstance(value["unique"], bool):
            raise StorageMigrationError("create-index unique must be boolean")
        return {
            "op": kind,
            "name": _identifier(value["name"], "index"),
            "table": _identifier(value["table"], "table"),
            "columns": columns,
            "unique": value["unique"],
        }
    if kind == "rename_table":
        _exact_keys(value, {"op", "from", "to"}, "rename-table operation")
        return {
            "op": kind,
            "from": _identifier(value["from"], "source table"),
            "to": _identifier(value["to"], "target table"),
        }
    if kind == "rename_column":
        _exact_keys(
            value, {"op", "table", "from", "to"}, "rename-column operation"
        )
        return {
            "op": kind,
            "table": _identifier(value["table"], "table"),
            "from": _identifier(value["from"], "source column"),
            "to": _identifier(value["to"], "target column"),
        }
    if kind == "drop_index":
        _exact_keys(value, {"op", "name"}, "drop-index operation")
        return {"op": kind, "name": _identifier(value["name"], "index")}
    raise StorageMigrationError("storage migration operation is unsupported")


def _normalize_column(value: Any, *, allow_primary_key: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageMigrationError("storage migration column must be an object")
    allowed = {"name", "type", "nullable", "primary_key", "default"}
    required = {"name", "type", "nullable", "primary_key"}
    if not required <= set(value) or not set(value) <= allowed:
        raise StorageMigrationError("storage migration column fields are invalid")
    name = _identifier(value["name"], "column")
    affinity = value["type"]
    nullable = value["nullable"]
    primary_key = value["primary_key"]
    if affinity not in _AFFINITIES:
        raise StorageMigrationError("storage migration column type is unsupported")
    if not isinstance(nullable, bool) or not isinstance(primary_key, bool):
        raise StorageMigrationError("storage migration column flags must be boolean")
    if primary_key and (not allow_primary_key or nullable):
        raise StorageMigrationError("storage migration primary key column is invalid")
    normalized: dict[str, Any] = {
        "name": name,
        "type": affinity,
        "nullable": nullable,
        "primary_key": primary_key,
    }
    if "default" in value:
        normalized["default"] = _normalize_default(value["default"])
    if not nullable and not primary_key and "default" not in normalized:
        raise StorageMigrationError("non-null migration column requires a default")
    return normalized


def _column_sql(column: Mapping[str, Any], *, allow_primary_key: bool = True) -> str:
    pieces = [_quote(column["name"]), column["type"]]
    if not column["nullable"]:
        pieces.append("NOT NULL")
    if column["primary_key"]:
        if not allow_primary_key:
            raise StorageMigrationError("primary key is invalid for this operation")
        pieces.append("PRIMARY KEY")
    if "default" in column:
        pieces.extend(("DEFAULT", _literal(column["default"])))
    return " ".join(pieces)


def _inspect_database(
    path: Path, *, new_database_schema: int
) -> tuple[int, Mapping[str, int], str]:
    connection = sqlite3.connect(path, timeout=30)
    try:
        return (
            _detect_schema(connection, new_database_schema),
            _table_counts(connection),
            _schema_sha256(connection),
        )
    finally:
        connection.close()


def _bootstrap_new_product_database(
    database_path: Path, manifest: StorageMigrationManifest
) -> None:
    """Materialize the compiled current catalog before Runtime activation.

    A future-schema candidate must carry a declarative bootstrap catalog
    understood by the admitting Runtime. The v2 manifest binds the result but
    does not make an older Runtime capable of inventing a newer catalog.
    """

    from .database import SCHEMA_VERSION, SQLiteDatabase

    if manifest.target_schema_version != SCHEMA_VERSION or manifest.steps:
        raise StorageMigrationError(
            "new product storage requires a supported signed bootstrap catalog"
        )
    SQLiteDatabase(database_path)


def _inspect_integrity(
    path: Path, *, new_database_schema: int
) -> tuple[int, Mapping[str, int], str, str, int]:
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema = _detect_schema(connection, new_database_schema)
        counts = _table_counts(connection)
        schema_sha256 = _schema_sha256(connection)
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        quick_check = "ok" if quick_rows == [("ok",)] else "failed"
        violations = 0
        cursor = connection.execute("PRAGMA foreign_key_check")
        for _row in cursor:
            violations += 1
            if violations > 10_000:
                break
        if quick_check != "ok" or violations:
            raise StorageMigrationError("storage migration integrity checks failed")
        return schema, counts, schema_sha256, quick_check, violations
    finally:
        connection.close()


def _detect_schema(connection: sqlite3.Connection, new_database_schema: int) -> int:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return new_database_schema
    if "runtime_meta" not in tables:
        raise StorageMigrationError("storage schema version table is missing")
    row = connection.execute(
        "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
    ).fetchone()
    if row is None:
        raise StorageMigrationError("storage schema version record is missing")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise StorageMigrationError("storage schema version record is invalid") from exc
    if not _schema_number(version):
        raise StorageMigrationError("storage schema version is outside product bounds")
    return version


def _schema_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE type IN ('table','index','trigger') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    if len(rows) > MAX_TABLES_IN_RECEIPT * 8:
        raise StorageMigrationError(
            "storage schema object count exceeds the receipt bound"
        )
    records: list[dict[str, str]] = []
    for object_type, name, table, sql in rows:
        if (
            object_type not in {"table", "index", "trigger"}
            or not isinstance(name, str)
            or not name
            or not isinstance(table, str)
            or not table
            or not isinstance(sql, str)
            or not sql.strip()
        ):
            raise StorageMigrationError("storage schema object definition is invalid")
        records.append(
            {
                "type": object_type,
                "name": name,
                "table": table,
                "sql": " ".join(sql.split()),
            }
        )
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_storage_schema_sha256() -> str:
    """Return the complete physical schema promised by this Runtime build."""

    from .database import SCHEMA_VERSION
    from .schema_catalog import compiled_product_schema_digest

    return _current_storage_schema_sha256(
        SCHEMA_VERSION,
        compiled_product_schema_digest(),
    )


@lru_cache(maxsize=8)
def _current_storage_schema_sha256(
    schema_version: int, product_schema_sha256: str
) -> str:
    del product_schema_sha256
    from .database import SCHEMA_VERSION, SQLiteDatabase

    if schema_version != SCHEMA_VERSION:
        raise StorageMigrationError("compiled Runtime storage version changed")
    with tempfile.TemporaryDirectory(prefix="ecorex-current-schema-") as root:
        database = SQLiteDatabase(Path(root) / "runtime.sqlite3")
        with database.reader() as connection:
            return _schema_sha256(connection)


def _table_counts(connection: sqlite3.Connection) -> Mapping[str, int]:
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    if len(names) > MAX_TABLES_IN_RECEIPT:
        raise StorageMigrationError("storage table count exceeds the receipt bound")
    counts: dict[str, int] = {}
    for name in names:
        if _SQL_IDENTIFIER.fullmatch(name) is None:
            raise StorageMigrationError("storage contains an unsafe table identifier")
        row = connection.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
            raise StorageMigrationError("storage table count is invalid")
        counts[name] = row[0]
    return MappingProxyType(counts)


def _copy_database_snapshot(source: Path, target: Path) -> None:
    if not source.exists() or source.stat().st_size == 0:
        return
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30
    )
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _validate_database_source(path: Path) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StorageMigrationError("storage database must be a regular non-link file")


def _write_receipt(path: Path, receipt: StorageMigrationReceipt) -> None:
    _validate_receipt_directory(path.parent, create=True)
    payload = receipt.to_bytes()
    if not 1 <= len(payload) <= MAX_STORAGE_MIGRATION_BYTES:
        raise StorageMigrationError("storage migration receipt size is invalid")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_regular_file(path: Path) -> bytes:
    metadata = path.lstat()
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise StorageMigrationError("storage migration receipt is not a regular file")
    if not 1 <= metadata.st_size <= MAX_STORAGE_MIGRATION_BYTES:
        raise StorageMigrationError("storage migration receipt size is invalid")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise StorageMigrationError("storage migration receipt changed while reading")
    return payload


def _validate_receipt_directory(path: Path, *, create: bool) -> None:
    if not os.path.lexists(path):
        if not create:
            return
        path.mkdir(parents=True, exist_ok=True)
    metadata = path.lstat()
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise StorageMigrationError(
            "storage migration receipt root must be a real directory"
        )


def _receipt_dict(
    *,
    phase: str,
    identity: StorageMigrationIdentity,
    plan_sha256: str,
    source_schema_version: int,
    target_schema_version: int,
    source_database_sha256: str,
    target_database_sha256: str,
    source_schema_sha256: str,
    target_schema_sha256: str,
    source_table_counts: Mapping[str, int],
    target_table_counts: Mapping[str, int],
    quick_check: str,
    foreign_key_violations: int,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": _STORAGE_MIGRATION_RECEIPT_SCHEMA_VERSION,
        "document_type": STORAGE_MIGRATION_RECEIPT_TYPE,
        "phase": phase,
        "release_id": identity.release_id,
        "build_digest": identity.build_digest,
        "artifact_id": identity.artifact_id,
        "artifact_sha256": identity.artifact_sha256,
        "plan_sha256": plan_sha256,
        "source_schema_version": source_schema_version,
        "target_schema_version": target_schema_version,
        "source_database_sha256": source_database_sha256,
        "target_database_sha256": target_database_sha256,
        "source_schema_sha256": source_schema_sha256,
        "target_schema_sha256": target_schema_sha256,
        "source_table_counts": dict(sorted(source_table_counts.items())),
        "target_table_counts": dict(sorted(target_table_counts.items())),
        "quick_check": quick_check,
        "foreign_key_violations": foreign_key_violations,
        "created_at": created_at,
    }


def _receipt_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        b"ecorex-storage-migration-receipt-v1\0" + _canonical_json_bytes(value)
    ).hexdigest()


def _load_canonical_json(payload: bytes, *, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_STORAGE_MIGRATION_BYTES:
        raise StorageMigrationError(f"{label} size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise StorageMigrationError(f"{label} must be unique-key UTF-8 JSON") from None
    if not isinstance(value, Mapping):
        raise StorageMigrationError(f"{label} must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise StorageMigrationError(f"{label} fields are invalid")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SQL_IDENTIFIER.fullmatch(value) is None:
        raise StorageMigrationError(f"storage migration {label} identifier is unsafe")
    return value


def _normalize_default(value: Any) -> None | bool | int | float | str:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise StorageMigrationError("storage migration column default is unsupported")


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "1"
    if value is False:
        return "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise StorageMigrationError("storage migration SQL literal is unsupported")


def _quote(value: str) -> str:
    if _SQL_IDENTIFIER.fullmatch(value) is None:
        raise StorageMigrationError("storage migration SQL identifier is unsafe")
    return '"' + value + '"'


def _validate_table_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > MAX_TABLES_IN_RECEIPT:
        raise StorageMigrationError("storage migration table counts are invalid")
    normalized: dict[str, int] = {}
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or _SQL_IDENTIFIER.fullmatch(key) is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise StorageMigrationError("storage migration table count is invalid")
        normalized[key] = count
    if list(normalized) != sorted(normalized):
        raise StorageMigrationError("storage migration table counts must be sorted")
    return normalized


def _schema_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= 64


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return _EMPTY_SHA256
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _constant_time_equal(first: str, second: Any) -> bool:
    return isinstance(second, str) and len(first) == len(second) and all(
        (ord(left) ^ ord(right)) == 0 for left, right in zip(first, second)
    )


__all__ = [
    "MAX_STORAGE_MIGRATION_BYTES",
    "STORAGE_MIGRATION_FILE_NAME",
    "StorageMigrationError",
    "StorageMigrationIdentity",
    "StorageMigrationManifest",
    "StorageMigrationReceipt",
    "apply_live_storage_migration",
    "current_storage_schema_sha256",
    "dry_run_storage_migration",
    "load_live_storage_migration_receipt",
    "migration_receipt_path",
]

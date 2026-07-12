"""Serializable contracts emitted by the migration service and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    relative_path: str
    kind: str
    size_bytes: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceInventory:
    source_version: str
    digest: str
    entries: tuple[InventoryEntry, ...]
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "digest": self.digest,
            "file_count": sum(1 for item in self.entries if item.kind == "file"),
            "entry_count": len(self.entries),
            "total_bytes": self.total_bytes,
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True, slots=True)
class BackupRecord:
    source_relative_path: str
    backup_relative_path: str
    source_sha256: str
    backup_sha256: str
    kind: str = "sqlite_snapshot"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MigrationWarning:
    code: str
    subject: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    migration_id: str
    status: str
    dry_run: bool
    idempotent_replay: bool
    source_version: str
    target_version: str
    storage_schema_version: int
    import_layout_version: int
    target_schema_sha256: str
    data_generation_id: str
    source_inventory_digest: str
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[MigrationWarning, ...] = ()
    backups: tuple[BackupRecord, ...] = ()
    sampled_artifact_ids: tuple[str, ...] = ()
    quarantine_entry_count: int = 0
    quarantine_summary: tuple[Mapping[str, Any], ...] = ()
    remaining_mappings: tuple[str, ...] = ()
    source_evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "idempotent_replay": self.idempotent_replay,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "storage_schema_version": self.storage_schema_version,
            "import_layout_version": self.import_layout_version,
            "target_schema_sha256": self.target_schema_sha256,
            "data_generation_id": self.data_generation_id,
            "source_inventory_digest": self.source_inventory_digest,
            "counts": dict(sorted(self.counts.items())),
            "warnings": [item.to_dict() for item in self.warnings],
            "backups": [item.to_dict() for item in self.backups],
            "sampled_artifact_ids": list(self.sampled_artifact_ids),
            "quarantine": {
                "entry_count": self.quarantine_entry_count,
                "activated": False,
                "uploaded": False,
                "summary": [dict(item) for item in self.quarantine_summary],
            },
            "remaining_mappings": list(self.remaining_mappings),
            "source_evidence": dict(self.source_evidence),
        }

"""Transport-safe projections for output preferences and materializations.

Absolute host paths deliberately do not appear in these contracts.  A client
selects one backend-owned location alias and receives only that alias back.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class OutputLocationAlias(str, Enum):
    DOCUMENTS = "documents"
    DOWNLOADS = "downloads"
    WORKSPACE = "workspace"


class MaterializationStatus(str, Enum):
    PREPARING = "preparing"
    PUBLISHED = "published"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class OutputLocationOption:
    alias: OutputLocationAlias
    available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias", OutputLocationAlias(self.alias))

    def to_dict(self) -> dict[str, Any]:
        return {"alias": self.alias.value, "available": self.available}


@dataclass(frozen=True, slots=True)
class OutputPreferenceProjection:
    account_id: str
    location_alias: OutputLocationAlias
    revision: int
    output_policy_snapshot_id: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "location_alias", OutputLocationAlias(self.location_alias))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "location_alias": self.location_alias.value,
            "revision": self.revision,
            "output_policy_snapshot_id": self.output_policy_snapshot_id,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class OutputPolicyProjection:
    output_policy_snapshot_id: str
    account_id: str
    preference_revision: int
    location_alias: OutputLocationAlias
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "location_alias", OutputLocationAlias(self.location_alias))

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_policy_snapshot_id": self.output_policy_snapshot_id,
            "account_id": self.account_id,
            "preference_revision": self.preference_revision,
            "location_alias": self.location_alias.value,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class MaterializationProjection:
    materialization_id: str
    artifact_id: str
    revision_id: str
    output_policy_snapshot_id: str
    location_alias: OutputLocationAlias
    display_name: str
    sha256: str
    size_bytes: int
    status: MaterializationStatus
    reused_existing: bool
    created_at: str
    completed_at: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "location_alias", OutputLocationAlias(self.location_alias))
        object.__setattr__(self, "status", MaterializationStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "materialization_id": self.materialization_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "output_policy_snapshot_id": self.output_policy_snapshot_id,
            "location_alias": self.location_alias.value,
            "display_name": self.display_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "status": self.status.value,
            "reused_existing": self.reused_existing,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class OutputAuditProjection:
    audit_id: str
    account_id: str
    action: str
    subject_id: str
    details: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "account_id": self.account_id,
            "action": self.action,
            "subject_id": self.subject_id,
            "details": dict(self.details),
            "created_at": self.created_at,
        }


__all__ = [
    "MaterializationProjection",
    "MaterializationStatus",
    "OutputAuditProjection",
    "OutputLocationAlias",
    "OutputLocationOption",
    "OutputPolicyProjection",
    "OutputPreferenceProjection",
]

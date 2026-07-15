"""Read-only contracts for restoring the release administrator workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import (
    CandidateProjection,
    DistributionProjection,
    KillSwitchProjection,
    RolloutProjection,
)


class ResumeStateProjection(BaseModel):
    """A bounded, internally consistent snapshot of the admin workspace.

    The selected records are named by explicit IDs. Consumers must never infer
    a current candidate or rollout from list order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    candidates: list[CandidateProjection] = Field(default_factory=list, max_length=200)
    latest_candidate_id: str | None = Field(default=None, min_length=1, max_length=128)
    rollouts: list[RolloutProjection] = Field(default_factory=list, max_length=500)
    latest_rollout_id: str | None = Field(default=None, min_length=1, max_length=128)
    channel_kill_switches: list[KillSwitchProjection] = Field(
        min_length=2,
        max_length=2,
    )
    distribution: DistributionProjection
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def _captured_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("resume state captured_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _facts_are_unambiguous(self) -> "ResumeStateProjection":
        candidate_ids = [item.release_id for item in self.candidates]
        rollout_ids = [item.rollout_id for item in self.rollouts]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("resume candidates must have unique release IDs")
        if len(rollout_ids) != len(set(rollout_ids)):
            raise ValueError("resume rollouts must have unique rollout IDs")
        self._validate_latest("candidate", candidate_ids, self.latest_candidate_id)
        self._validate_latest("rollout", rollout_ids, self.latest_rollout_id)

        channels = [item.channel for item in self.channel_kill_switches]
        if len(channels) != len(set(channels)) or set(channels) != {"canary", "stable"}:
            raise ValueError("resume state requires exactly one fact for each release channel")
        for item in self.channel_kill_switches:
            if len(item.halted_rollout_ids) != len(set(item.halted_rollout_ids)):
                raise ValueError("halted rollout IDs must be unique per channel")

        distribution = self.distribution
        if distribution.total_clients < 0:
            raise ValueError("distribution total must not be negative")
        for counts in (distribution.versions, distribution.update_states):
            if any(not key or value < 0 for key, value in counts.items()):
                raise ValueError("distribution keys and counts must be valid")
            if sum(counts.values()) != distribution.total_clients:
                raise ValueError("distribution counts must equal the client total")
        return self

    @staticmethod
    def _validate_latest(kind: str, identifiers: list[str], latest: str | None) -> None:
        if not identifiers and latest is not None:
            raise ValueError(f"latest {kind} ID cannot exist without {kind} facts")
        if identifiers and latest is None:
            raise ValueError(f"latest {kind} ID is required when {kind} facts exist")
        if latest is not None and latest not in identifiers:
            raise ValueError(f"latest {kind} ID must reference a returned fact")


class AdminResumeProvider(Protocol):
    """Loads one server-authoritative resume snapshot."""

    def resume_state(self) -> ResumeStateProjection:
        ...


@dataclass(frozen=True, slots=True)
class AdminResumeFacts:
    """Repository-neutral facts loaded within one consistency boundary."""

    candidates: Sequence[CandidateProjection]
    latest_candidate_id: str | None
    rollouts: Sequence[RolloutProjection]
    latest_rollout_id: str | None
    channel_kill_switches: Sequence[KillSwitchProjection]
    distribution: DistributionProjection
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class AdminResumeAdapter:
    """Validates an atomic repository snapshot at the HTTP trust boundary.

    ``load_facts`` should perform all reads in one database read transaction.
    This adapter deliberately does not infer latest IDs or reorder records.
    """

    load_facts: Callable[
        [],
        AdminResumeFacts | ResumeStateProjection | Mapping[str, object],
    ]

    def resume_state(self) -> ResumeStateProjection:
        facts = self.load_facts()
        if isinstance(facts, AdminResumeFacts):
            facts = {
                "schema_version": 1,
                "candidates": list(facts.candidates),
                "latest_candidate_id": facts.latest_candidate_id,
                "rollouts": list(facts.rollouts),
                "latest_rollout_id": facts.latest_rollout_id,
                "channel_kill_switches": list(facts.channel_kill_switches),
                "distribution": facts.distribution,
                "captured_at": facts.captured_at,
            }
        return ResumeStateProjection.model_validate(facts)


__all__ = [
    "AdminResumeAdapter",
    "AdminResumeFacts",
    "AdminResumeProvider",
    "ResumeStateProjection",
]

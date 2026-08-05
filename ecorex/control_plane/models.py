"""Control Plane transport models and authentication boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ecorex.product_version import is_stable_release_version


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class ControlPrincipal:
    subject: str
    client_id: str
    account_id: str
    organization_id: str | None = None
    roles: frozenset[str] = frozenset()
    token_id: str | None = None


class ControlPlaneAuthenticator(Protocol):
    def authenticate(self, bearer_token: str) -> ControlPrincipal: ...


class RejectingControlPlaneAuthenticator:
    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        del bearer_token
        raise PermissionError("Control Plane authentication is not configured")


class CreateCandidateRequest(ControlModel):
    manifest: dict
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=1, max_length=256)


class GateResultRequest(ControlModel):
    status: Literal["passed", "failed"]
    evidence: str = Field(default="", max_length=4096)
    client_request_id: str = Field(min_length=1, max_length=256)


class GateBundleRequest(ControlModel):
    attestation: dict
    client_request_id: str = Field(min_length=1, max_length=256)


class DirectAdmissionRequest(ControlModel):
    attestation: dict
    client_request_id: str = Field(min_length=1, max_length=256)


class CreateRolloutRequest(ControlModel):
    release_id: str = Field(min_length=1, max_length=128)
    percentage: int = Field(ge=1, le=100)
    target_organization_ids: list[str] = Field(default_factory=list, max_length=500)
    target_account_ids: list[str] = Field(default_factory=list, max_length=500)
    minimum_compatible_version: str | None = Field(default=None, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=256)


class CreateRollbackRequest(ControlModel):
    source_release_id: str = Field(min_length=1, max_length=128)
    target_release_id: str = Field(min_length=1, max_length=128)
    percentage: int = Field(ge=1, le=100)
    target_organization_ids: list[str] = Field(default_factory=list, max_length=500)
    target_account_ids: list[str] = Field(default_factory=list, max_length=500)
    authorization_ttl_seconds: int = Field(default=300, ge=60, le=900)
    client_request_id: str = Field(min_length=1, max_length=256)


class RolloutActionRequest(ControlModel):
    client_request_id: str = Field(min_length=1, max_length=256)


class BootstrapIndexTargetProjection(ControlModel):
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_id: str = Field(pattern=r"^release-stable-[0-9a-f]{24}$")
    version: str
    build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not is_stable_release_version(value):
            raise ValueError("version must be a final product SemVer")
        return value


class BootstrapIndexProofProjection(ControlModel):
    schema_version: Literal[1]
    record_id: str = Field(pattern=r"^bread_[0-9a-f]{32}$")
    activation_record_id: str = Field(pattern=r"^bactive_[0-9a-f]{32}$")
    stage_record_id: str = Field(pattern=r"^bstage_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^release-stable-[0-9a-f]{24}$")
    version: str
    build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    revision: str = Field(pattern=r"^release-stable-[0-9a-f]{24}$")
    issued_at: str
    expires_at: str
    target: BootstrapIndexTargetProjection
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_size_bytes: int = Field(ge=1, le=256 * 1024)
    public_url: str
    read_back_at: str
    proof_token: str = Field(
        pattern=(r"^bootstrap-index-proof:bread_[0-9a-f]{32}:sha256:[0-9a-f]{64}$")
    )


class BootstrapFreshnessStatusProjection(ControlModel):
    schema_version: Literal[1]
    status: Literal["idle", "healthy", "refreshing", "degraded", "unconfigured"]
    active_expires_at: str | None
    active_authority_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    remaining_seconds: int | None
    last_checked_at: str | None
    next_check_at: str | None
    last_attempt_record_id: str | None
    last_success_at: str | None
    last_failure_at: str | None
    last_error_code: str | None
    lease_owner_id: str | None
    lease_expires_at: str | None
    updated_at: str | None
    automation_enabled: bool
    signer_configured: bool
    lead_seconds: int
    check_interval_seconds: int
    lease_seconds: int
    scheduler_running: bool
    scheduler_ready: bool
    scheduler_last_heartbeat_at: str | None
    scheduler_last_error_code: str | None
    scheduler_heartbeat_max_age_seconds: int = Field(ge=1)


class BootstrapFreshnessRunProjection(BootstrapFreshnessStatusProjection):
    run_state: Literal[
        "succeeded",
        "not-due",
        "no-active",
        "busy",
        "failed",
        "unconfigured",
    ]


class CandidateProjection(ControlModel):
    release_id: str
    version: str
    build_digest: str
    channel: str
    status: str
    gates: dict[str, str]
    missing_gates: list[str]


class RolloutProjection(ControlModel):
    rollout_id: str
    release_id: str
    channel: str
    status: str
    percentage: int
    target_organization_ids: list[str]
    target_account_ids: list[str]
    minimum_compatible_version: str | None
    created_at: str


class RollbackProjection(ControlModel):
    rollback_id: str
    source_release_id: str
    target_release_id: str
    channel: str
    status: str
    percentage: int
    target_organization_ids: list[str]
    target_account_ids: list[str]
    authorization_ttl_seconds: int
    created_at: str


class DistributionProjection(ControlModel):
    total_clients: int
    versions: dict[str, int]
    update_states: dict[str, int]


class KillSwitchProjection(ControlModel):
    channel: Literal["canary", "stable"]
    halted_rollout_ids: list[str]
    kill_switch_active: bool = True


class ControlUpdateSignal(ControlModel):
    """A non-authoritative, durable wake-up fact for update clients.

    The signal deliberately contains release and rollout identities only.  It
    never carries administrator identity, bearer material, account IDs, or
    organization IDs; client eligibility is evaluated from canonical rollout
    state immediately before a local WebSocket delivery.
    """

    sequence: int = Field(ge=1)
    event_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    signal_type: Literal[
        "rollout.activated",
        "rollout.paused",
        "rollout.halted",
        "channel.killed",
        "channel.kill_cleared",
    ]
    channel: Literal["canary", "stable"]
    rollout_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    release_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    created_at: str = Field(min_length=20, max_length=64)


class ControlUpdateSignalBatch(ControlModel):
    after_sequence: int = Field(ge=0)
    retained_floor_sequence: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    gap_detected: bool
    signals: list[ControlUpdateSignal] = Field(max_length=256)

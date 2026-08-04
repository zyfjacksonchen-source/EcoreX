"""Public v1 protocol models.

The models in this module are deliberately transport-only.  Runtime services own
all policy and state changes; clients may submit intent but never authoritative
state.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


JsonObject = dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc)


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class FrozenProtocolModel(ProtocolModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class ThreadStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    PREPARING = "preparing"
    MODEL_REQUESTED = "model_requested"
    STREAMING = "streaming"
    TOOL_PENDING = "tool_pending"
    WAITING_HUMAN = "waiting_human"
    TOOL_RUNNING = "tool_running"
    RETRY_WAIT = "retry_wait"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"


TERMINAL_TURN_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.INTERRUPTED,
        TurnStatus.SUPERSEDED,
    }
)


TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.ACCEPTED: frozenset(
        {TurnStatus.QUEUED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED}
    ),
    TurnStatus.QUEUED: frozenset(
        {
            TurnStatus.PREPARING,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.PREPARING: frozenset(
        {
            TurnStatus.MODEL_REQUESTED,
            TurnStatus.TOOL_PENDING,
            TurnStatus.WAITING_HUMAN,
            TurnStatus.RETRY_WAIT,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.MODEL_REQUESTED: frozenset(
        {
            TurnStatus.STREAMING,
            TurnStatus.TOOL_PENDING,
            TurnStatus.WAITING_HUMAN,
            TurnStatus.RETRY_WAIT,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.STREAMING: frozenset(
        {
            TurnStatus.TOOL_PENDING,
            TurnStatus.RETRY_WAIT,
            TurnStatus.FINALIZING,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.TOOL_PENDING: frozenset(
        {
            TurnStatus.WAITING_HUMAN,
            TurnStatus.TOOL_RUNNING,
            # A leased worker may die after committing TOOL_PENDING but before
            # its tool checkpoint. Lease recovery must return authority to the
            # durable retry path instead of stranding the Turn forever.
            TurnStatus.RETRY_WAIT,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.WAITING_HUMAN: frozenset(
        {
            TurnStatus.PREPARING,
            TurnStatus.TOOL_RUNNING,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.TOOL_RUNNING: frozenset(
        {
            TurnStatus.STREAMING,
            TurnStatus.TOOL_PENDING,
            # A non-idempotent process may lose its acknowledgement after it
            # started.  Core must persist an uncertainty HITL fence instead
            # of auto-retrying or failing away the user's decision point.
            TurnStatus.WAITING_HUMAN,
            TurnStatus.RETRY_WAIT,
            TurnStatus.FINALIZING,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.RETRY_WAIT: frozenset(
        {
            TurnStatus.PREPARING,
            TurnStatus.MODEL_REQUESTED,
            TurnStatus.TOOL_RUNNING,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    TurnStatus.FINALIZING: frozenset(
        {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
            TurnStatus.SUPERSEDED,
        }
    ),
    **{status: frozenset() for status in TERMINAL_TURN_STATUSES},
}


class ItemKind(str, Enum):
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    ARTIFACT = "artifact"
    INTERACTION = "interaction"
    CHECKPOINT = "checkpoint"
    TASK_LIST = "task_list"


class ReasoningPresentation(str, Enum):
    """Backend-owned presentation state for a disclosed reasoning summary."""

    VISIBLE = "visible"
    COLLAPSED = "collapsed"
    ARCHIVED = "archived"


class ReasoningItemContent(FrozenProtocolModel):
    """Typed content stored by a durable reasoning Item.

    This contract carries provider-approved reasoning summaries only.  Hidden
    chain-of-thought is never accepted into the public event stream.
    """

    channel: Literal["reasoning_summary"] = "reasoning_summary"
    atom_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=1_000_000)
    revision: int = Field(ge=1)
    presentation: ReasoningPresentation
    archived_reason: str | None = Field(default=None, max_length=256)


class TaskListEntry(FrozenProtocolModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    title: str = Field(min_length=1, max_length=240)
    status: Literal["pending", "in_progress", "completed"]


class TaskListProjection(FrozenProtocolModel):
    schema_version: Literal[1] = 1
    turn_id: str = Field(min_length=1, max_length=256)
    items: tuple[TaskListEntry, ...] = Field(min_length=2, max_length=8)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_items(self) -> "TaskListProjection":
        identities = [item.id for item in self.items]
        if len(set(identities)) != len(identities):
            raise ValueError("Task List item ids must be unique")
        if sum(item.status == "in_progress" for item in self.items) > 1:
            raise ValueError("Task List permits at most one in-progress item")
        return self


class ItemStatus(str, Enum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_ITEM_STATUSES = frozenset(
    {ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED}
)


ITEM_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    ItemStatus.CREATED: frozenset(
        {
            ItemStatus.IN_PROGRESS,
            ItemStatus.WAITING_HUMAN,
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        }
    ),
    ItemStatus.IN_PROGRESS: frozenset(
        {
            ItemStatus.WAITING_HUMAN,
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        }
    ),
    ItemStatus.WAITING_HUMAN: frozenset(
        {
            ItemStatus.IN_PROGRESS,
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.CANCELLED,
        }
    ),
    **{status: frozenset() for status in TERMINAL_ITEM_STATUSES},
}


class JobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.DEAD_LETTER,
    }
)


JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.LEASED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.LEASED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.QUEUED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTER,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.WAITING_HUMAN,
            JobStatus.RETRY_SCHEDULED,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.QUEUED,
            JobStatus.DEAD_LETTER,
        }
    ),
    JobStatus.WAITING_HUMAN: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.RETRY_SCHEDULED: frozenset(
        {
            JobStatus.LEASED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTER,
        }
    ),
    **{status: frozenset() for status in TERMINAL_JOB_STATUSES},
}


class InteractionKind(str, Enum):
    PERMISSION_APPROVAL = "permission_approval"
    INFORMATION = "information"
    CONNECTOR_LOGIN = "connector_login"
    CONFLICT_RESOLUTION = "conflict_resolution"
    ARTIFACT_REVIEW = "artifact_review"


class InteractionStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InteractionFieldControl(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"


class InteractionActionType(str, Enum):
    SUBMIT = "submit"
    CONTINUE = "continue"
    CANCEL = "cancel"
    ALLOW = "allow"
    DENY = "deny"
    RETRY = "retry"
    SKIP = "skip"
    ACCEPT = "accept"
    REQUEST_CHANGES = "request_changes"
    CONNECTOR_BEGIN_LOGIN = "connector_begin_login"
    CONNECTOR_CHECK_STATUS = "connector_check_status"


class InteractionActionStyle(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DANGER = "danger"


class ConnectorInteractionState(str, Enum):
    AUTHORIZATION_REQUIRED = "authorization_required"
    AWAITING_CALLBACK = "awaiting_callback"
    VERIFYING = "verifying"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


_INTERACTION_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
_SENSITIVE_INTERACTION_FIELD_NAMES = frozenset(
    {
        "password",
        "passcode",
        "pin",
        "otp",
        "onetimepassword",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "secret",
        "secretkey",
        "clientsecret",
        "privatekey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "密码",
        "口令",
        "令牌",
        "密钥",
        "凭证",
        "验证码",
    }
)
_SENSITIVE_INTERACTION_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\bsk-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|\bAKIA[A-Z0-9]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"(?:access_token|refresh_token|token|secret|code)=[^&\s]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def _interaction_sensitive_name(value: str) -> bool:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()
    return any(candidate in normalized for candidate in _SENSITIVE_INTERACTION_FIELD_NAMES)


class InteractionChoice(FrozenProtocolModel):
    option_id: str = Field(pattern=_INTERACTION_ID_PATTERN)
    label: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=240)


class InteractionFormField(FrozenProtocolModel):
    field_id: str = Field(pattern=_INTERACTION_ID_PATTERN)
    label: str = Field(min_length=1, max_length=80)
    control: InteractionFieldControl
    required: bool = False
    description: str | None = Field(default=None, max_length=240)
    placeholder: str | None = Field(default=None, max_length=160)
    min_length: int = Field(default=0, ge=0, le=4_000)
    max_length: int = Field(default=500, ge=1, le=4_000)
    options: list[InteractionChoice] = Field(default_factory=list, max_length=100)
    sensitive: Literal[False] = False

    @model_validator(mode="after")
    def _validate_control_contract(self) -> "InteractionFormField":
        if any(
            _interaction_sensitive_name(value)
            for value in (
                self.field_id,
                self.label,
                self.description or "",
                self.placeholder or "",
            )
        ):
            raise ValueError("sensitive interaction fields are not persistable")
        if self.min_length > self.max_length:
            raise ValueError("interaction field min_length exceeds max_length")
        option_ids = [option.option_id for option in self.options]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("interaction field option IDs must be unique")
        if self.control is InteractionFieldControl.SELECT:
            if not self.options:
                raise ValueError("select interaction fields require options")
        elif self.options:
            raise ValueError("only select interaction fields may declare options")
        if self.control is InteractionFieldControl.CHECKBOX and (
            self.min_length != 0 or self.max_length != 500
        ):
            raise ValueError("checkbox interaction fields cannot declare text bounds")
        return self


class InteractionAction(FrozenProtocolModel):
    action_id: str = Field(pattern=_INTERACTION_ID_PATTERN)
    label: str = Field(min_length=1, max_length=80)
    action_type: InteractionActionType
    style: InteractionActionStyle = InteractionActionStyle.SECONDARY
    submits_form: bool = False


class InteractionConnectorContext(FrozenProtocolModel):
    connector_id: str = Field(pattern=_INTERACTION_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=100)
    state: ConnectorInteractionState
    required_action_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("required_action_ids")
    @classmethod
    def _validate_required_actions(cls, value: list[str]) -> list[str]:
        if (
            len(set(value)) != len(value)
            or any(re.fullmatch(_INTERACTION_ID_PATTERN, item) is None for item in value)
        ):
            raise ValueError("connector required action IDs are invalid")
        return value


class InteractionContract(FrozenProtocolModel):
    """Versioned, persistable UI/action contract owned by Runtime.

    The contract deliberately cannot describe password controls or secret
    fields. Connector authentication is represented only by safe lifecycle
    actions and a backend-owned public status.
    """

    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=120)
    fields: list[InteractionFormField] = Field(default_factory=list, max_length=16)
    actions: list[InteractionAction] = Field(min_length=1, max_length=8)
    connector: InteractionConnectorContext | None = None

    @model_validator(mode="after")
    def _validate_identity(self) -> "InteractionContract":
        field_ids = [field.field_id for field in self.fields]
        action_ids = [action.action_id for action in self.actions]
        if len(set(field_ids)) != len(field_ids):
            raise ValueError("interaction field IDs must be unique")
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("interaction action IDs must be unique")
        return self

    def validate_for_kind(self, kind: InteractionKind) -> "InteractionContract":
        action_types = {action.action_type for action in self.actions}
        allowed: dict[InteractionKind, frozenset[InteractionActionType]] = {
            InteractionKind.PERMISSION_APPROVAL: frozenset(
                {
                    InteractionActionType.ALLOW,
                    InteractionActionType.DENY,
                    InteractionActionType.CANCEL,
                }
            ),
            InteractionKind.INFORMATION: frozenset(
                {
                    InteractionActionType.SUBMIT,
                    InteractionActionType.CONTINUE,
                    InteractionActionType.CANCEL,
                }
            ),
            InteractionKind.CONNECTOR_LOGIN: frozenset(
                {
                    InteractionActionType.CONNECTOR_BEGIN_LOGIN,
                    InteractionActionType.CONNECTOR_CHECK_STATUS,
                    InteractionActionType.CANCEL,
                }
            ),
            InteractionKind.CONFLICT_RESOLUTION: frozenset(
                {
                    InteractionActionType.RETRY,
                    InteractionActionType.SKIP,
                    InteractionActionType.CONTINUE,
                    InteractionActionType.CANCEL,
                }
            ),
            InteractionKind.ARTIFACT_REVIEW: frozenset(
                {
                    InteractionActionType.ACCEPT,
                    InteractionActionType.REQUEST_CHANGES,
                    InteractionActionType.CANCEL,
                }
            ),
        }
        if not action_types <= allowed[kind]:
            raise ValueError(f"interaction actions are invalid for {kind.value}")
        if kind is InteractionKind.PERMISSION_APPROVAL:
            if self.fields or not {
                InteractionActionType.ALLOW,
                InteractionActionType.DENY,
            } <= action_types:
                raise ValueError("permission interactions require allow/deny and no fields")
        if kind is InteractionKind.CONNECTOR_LOGIN:
            if self.fields or self.connector is None:
                raise ValueError(
                    "connector login interactions allow safe actions/status only"
                )
        elif self.connector is not None:
            raise ValueError("connector status is valid only for connector login")
        if self.fields and not any(action.submits_form for action in self.actions):
            raise ValueError("interaction fields require a form-submitting action")
        return self

    def validate_response(self, response: "InteractionResponse") -> "InteractionResponse":
        action = next(
            (candidate for candidate in self.actions if candidate.action_id == response.action_id),
            None,
        )
        if action is None:
            raise ValueError("interaction response must select a declared action")
        if not action.submits_form:
            if response.values:
                raise ValueError("this interaction action does not accept form values")
            return response
        fields = {field.field_id: field for field in self.fields}
        extra = set(response.values) - set(fields)
        if extra:
            raise ValueError(
                "interaction response contains undeclared fields: "
                + ", ".join(sorted(extra))
            )
        for field in self.fields:
            value = response.values.get(field.field_id)
            if value is None:
                if field.required:
                    raise ValueError(f"interaction field {field.field_id!r} is required")
                continue
            if field.control is InteractionFieldControl.CHECKBOX:
                if type(value) is not bool:
                    raise ValueError(
                        f"interaction field {field.field_id!r} requires a boolean"
                    )
                if field.required and value is not True:
                    raise ValueError(
                        f"interaction field {field.field_id!r} must be checked"
                    )
                continue
            if type(value) is not str:
                raise ValueError(
                    f"interaction field {field.field_id!r} requires text"
                )
            if not field.min_length <= len(value) <= field.max_length:
                raise ValueError(
                    f"interaction field {field.field_id!r} has invalid length"
                )
            if field.control is InteractionFieldControl.SELECT and value not in {
                option.option_id for option in field.options
            }:
                raise ValueError(
                    f"interaction field {field.field_id!r} must select a declared option"
                )
            if _SENSITIVE_INTERACTION_VALUE.search(value):
                raise ValueError("sensitive values cannot be persisted in interactions")
        return response


class InteractionResponse(FrozenProtocolModel):
    action_id: str = Field(pattern=_INTERACTION_ID_PATTERN)
    values: dict[str, str | bool] = Field(default_factory=dict, max_length=16)

    @field_validator("values", mode="before")
    @classmethod
    def _strict_scalar_values(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("interaction values must be an object")
        for field_id, field_value in value.items():
            if not isinstance(field_id, str) or re.fullmatch(
                _INTERACTION_ID_PATTERN, field_id
            ) is None:
                raise ValueError("interaction response field ID is invalid")
            if type(field_value) not in {str, bool}:
                raise ValueError("interaction response values must be text or boolean")
        return value


class ToolInteractionDirective(FrozenProtocolModel):
    """Trusted tool-output directive consumed by the Agent worker."""

    schema_version: Literal[1] = 1
    kind: Literal[
        InteractionKind.INFORMATION,
        InteractionKind.CONNECTOR_LOGIN,
        InteractionKind.CONFLICT_RESOLUTION,
        InteractionKind.ARTIFACT_REVIEW,
    ]
    prompt: str = Field(min_length=1, max_length=2_000)
    contract: InteractionContract

    @model_validator(mode="after")
    def _validate_contract_kind(self) -> "ToolInteractionDirective":
        self.contract.validate_for_kind(InteractionKind(self.kind))
        return self


class PublicArtifactRef(FrozenProtocolModel):
    """Opaque, user-addressable Artifact identity safe for public tool activity."""

    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$",
    )
    revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$",
    )


class PublicToolActivity(FrozenProtocolModel):
    """The only Tool Item/Event body that may cross the public Runtime API.

    Raw arguments, results, paths, provider bodies and execution fencing data
    live in ToolExecutionRecord/Worker checkpoint storage.  This contract is a
    deliberately small product projection generated by reviewed backend code.
    """

    schema_version: Literal[1] = 1
    tool_call_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$",
    )
    tool_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$",
    )
    # Temporary wire compatibility for the existing Web timeline.  Validation
    # requires the same canonical value; aliases/provider labels cannot enter.
    tool_name: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$",
    )
    display_label: str = Field(min_length=1, max_length=80)
    phase: Literal[
        "requested",
        "running",
        "waiting_human",
        "completed",
        "failed",
        "cancelled",
    ]
    status: Literal[
        "created",
        "in_progress",
        "waiting_human",
        "completed",
        "failed",
        "cancelled",
    ]
    effects: list[
        Literal[
            "read",
            "write",
            "network",
            "execute",
            "ui_automation",
            "generate_media",
        ]
    ] = Field(default_factory=list, max_length=6)
    risk: Literal["low", "medium", "high"]
    argument_summary: str = Field(min_length=1, max_length=160)
    result_summary: str | None = Field(default=None, min_length=1, max_length=160)
    argument_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_refs: list[PublicArtifactRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_public_tool_state(self) -> "PublicToolActivity":
        if self.tool_name != self.tool_id:
            raise ValueError("public tool name must be canonical")
        if self.effects != sorted(set(self.effects)):
            raise ValueError("public tool effects must be sorted and unique")
        allowed_phases = {
            "created": {"requested"},
            "in_progress": {"requested", "running"},
            "waiting_human": {"waiting_human"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if self.phase not in allowed_phases[self.status]:
            raise ValueError("public tool phase and status are inconsistent")
        identities = [
            (reference.artifact_id, reference.revision_id)
            for reference in self.artifact_refs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("public Artifact references must be unique")
        return self


class EventEnvelope(FrozenProtocolModel):
    """One immutable fact in a thread's ordered event stream."""

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    thread_id: str = Field(min_length=1)
    turn_id: str | None = None
    item_id: str | None = None
    job_id: str | None = None
    tool_call_id: str | None = None
    client_message_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    config_snapshot_id: str | None = None
    capability_snapshot_id: str | None = None
    permission_snapshot_id: str | None = None
    extension_snapshot_id: str | None = None
    event_type: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    payload: JsonObject = Field(default_factory=dict)

    _created_at_utc = field_validator("created_at")(_ensure_utc)


class ThreadProjection(FrozenProtocolModel):
    thread_id: str
    status: ThreadStatus
    title: str | None = None
    pinned: bool = False
    active_turn_status: TurnStatus | None = None
    last_turn_status: TurnStatus | None = None
    metadata: JsonObject = Field(default_factory=dict)
    forked_from_thread_id: str | None = None
    forked_from_turn_id: str | None = None
    forked_from_seq: int | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator("created_at", "updated_at")(_ensure_utc)


class ProjectProjection(FrozenProtocolModel):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    project_path: str = Field(min_length=1, max_length=4096)
    pinned: bool = False
    thread_count: int = Field(default=0, ge=0)
    created_at: str = ""
    updated_at: str = ""


class ProjectListResponse(FrozenProtocolModel):
    projects: list[ProjectProjection] = Field(default_factory=list)


class PickProjectFolderRequest(ProtocolModel):
    client_request_id: str = Field(min_length=1, max_length=256)


class InputAttachmentProjection(FrozenProtocolModel):
    """Safe, opaque reference to a user-selected input file.

    Input attachments are internal source Artifacts.  Their identities may be
    attached to a Turn, but they never enter the office deliverables list.
    """

    attachment_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0, le=64 * 1024 * 1024)
    media_kind: Literal["image", "document", "file"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Relative authenticated endpoint.  The URL deliberately contains only
    # the opaque account-scoped attachment identity, never a CAS path.
    thumbnail_url: str | None = Field(default=None, min_length=1, max_length=512)
    created_at: datetime

    _created_at_utc = field_validator("created_at")(_ensure_utc)


class TurnProjection(FrozenProtocolModel):
    turn_id: str
    thread_id: str
    status: TurnStatus
    input: str
    agent_model_id: str
    image_model_id: str | None = None
    client_message_id: str | None = None
    metadata: JsonObject = Field(default_factory=dict)
    inherited: bool = False
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator("created_at", "updated_at")(_ensure_utc)


class TurnInputRevision(FrozenProtocolModel):
    """One immutable user-intent revision accepted by a running Turn."""

    revision_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    source: Literal["initial", "steer", "authority_refresh"]
    input: str = Field(min_length=1)
    agent_model_id: str = Field(min_length=1, max_length=256)
    image_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    explicit_tool_ids: list[str] = Field(default_factory=list, max_length=64)
    metadata: JsonObject = Field(default_factory=dict)
    client_message_id: str | None = None
    intent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    _created_at_utc = field_validator("created_at")(_ensure_utc)


class TurnExecutionBatch(FrozenProtocolModel):
    """Immutable binding between an input-revision range and Runtime authority."""

    batch_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    first_revision_ordinal: int = Field(ge=0)
    last_revision_ordinal: int = Field(ge=0)
    config_snapshot_id: str = Field(min_length=1)
    capability_snapshot_id: str = Field(min_length=1)
    permission_snapshot_id: str = Field(min_length=1)
    model_catalog_snapshot_id: str = Field(min_length=1)
    extension_snapshot_id: str = Field(min_length=1)
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    _created_at_utc = field_validator("created_at")(_ensure_utc)

    @model_validator(mode="after")
    def _validate_revision_range(self) -> "TurnExecutionBatch":
        if self.last_revision_ordinal < self.first_revision_ordinal:
            raise ValueError("execution batch revision range is invalid")
        return self


class ItemProjection(FrozenProtocolModel):
    item_id: str
    thread_id: str
    turn_id: str
    kind: ItemKind
    status: ItemStatus
    content: JsonObject = Field(default_factory=dict)
    inherited: bool = False
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator("created_at", "updated_at")(_ensure_utc)


class DurableJob(FrozenProtocolModel):
    job_id: str
    kind: str
    payload: JsonObject = Field(default_factory=dict)
    status: JobStatus
    priority: int = 0
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    thread_id: str | None = None
    turn_id: str | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    available_at: datetime
    deadline: datetime | None = None
    checkpoint: JsonObject | None = None
    idempotency_key: str
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator(
        "lease_expires_at",
        "heartbeat_at",
        "available_at",
        "deadline",
        "created_at",
        "updated_at",
    )(_ensure_utc)


def _public_job_reason_code(job: DurableJob) -> str | None:
    """Return a bounded user-safe reason without exposing Runtime errors.

    ``last_error`` may contain provider responses, local paths, connector
    details, or other diagnostic material.  Public projections deliberately
    collapse it to a small product vocabulary; the original value remains in
    the local event/audit stores for authorized diagnostics.
    """

    if job.status is JobStatus.CANCELLED:
        return "cancelled"
    if job.status is JobStatus.RETRY_SCHEDULED:
        return "retry_scheduled"
    if job.status is JobStatus.DEAD_LETTER:
        return "attempts_exhausted"
    if job.status is JobStatus.FAILED:
        if job.last_error == "deadline_exceeded":
            return "deadline_exceeded"
        return "execution_failed"
    return None


class JobProjection(FrozenProtocolModel):
    """Safe Web projection of a durable job.

    Lease fencing data, checkpoints, idempotency keys, raw payloads, worker
    identity, heartbeat details, and raw failures are Runtime-internal and can
    never cross this contract.
    """

    job_id: str
    kind: str
    status: JobStatus
    priority: int = 0
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    thread_id: str | None = None
    turn_id: str | None = None
    available_at: datetime
    deadline: datetime | None = None
    reason_code: str | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator(
        "available_at", "deadline", "created_at", "updated_at"
    )(_ensure_utc)

    @model_validator(mode="before")
    @classmethod
    def _from_durable_job(cls, value: Any) -> Any:
        if not isinstance(value, DurableJob):
            return value
        return {
            "job_id": value.job_id,
            "kind": value.kind,
            "status": value.status,
            "priority": value.priority,
            "attempt": value.attempt,
            "max_attempts": value.max_attempts,
            "thread_id": value.thread_id,
            "turn_id": value.turn_id,
            "available_at": value.available_at,
            "deadline": value.deadline,
            "reason_code": _public_job_reason_code(value),
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }

    @property
    def lease_token(self) -> None:
        """Compatibility sentinel; never serialized into the public contract."""

        return None


class InteractionRequest(FrozenProtocolModel):
    interaction_id: str
    kind: InteractionKind
    status: InteractionStatus
    prompt: str
    contract: InteractionContract
    options: list[JsonObject] = Field(default_factory=list)
    response: InteractionResponse | None = None
    response_client_request_id: str | None = None
    thread_id: str
    turn_id: str | None = None
    job_id: str | None = None
    idempotency_key: str
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator(
        "expires_at", "created_at", "updated_at"
    )(_ensure_utc)


class InteractionProjection(FrozenProtocolModel):
    """Safe interaction card returned to the WebUI."""

    interaction_id: str
    kind: InteractionKind
    status: InteractionStatus
    prompt: str
    contract: InteractionContract
    options: list[JsonObject] = Field(default_factory=list)
    response: InteractionResponse | None = None
    response_client_request_id: str | None = None
    thread_id: str
    turn_id: str | None = None
    job_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    _timestamps_utc = field_validator(
        "expires_at", "created_at", "updated_at"
    )(_ensure_utc)

    @model_validator(mode="before")
    @classmethod
    def _from_interaction_request(cls, value: Any) -> Any:
        if not isinstance(value, InteractionRequest):
            return value
        return {
            "interaction_id": value.interaction_id,
            "kind": value.kind,
            "status": value.status,
            "prompt": value.prompt,
            "contract": value.contract,
            "options": value.options,
            "response": value.response,
            "response_client_request_id": value.response_client_request_id,
            "thread_id": value.thread_id,
            "turn_id": value.turn_id,
            "job_id": value.job_id,
            "expires_at": value.expires_at,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }


class RespondInteractionRequest(ProtocolModel):
    response: InteractionResponse
    client_request_id: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$",
    )


class InteractionMutationResponse(ProtocolModel):
    interaction: InteractionProjection
    turn: TurnProjection | None = None
    job: JobProjection | None = None
    watermark: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_related_identity(self) -> "InteractionMutationResponse":
        if self.turn is not None:
            if self.turn.thread_id != self.interaction.thread_id:
                raise ValueError("interaction mutation Turn belongs to another Thread")
            if (
                self.interaction.turn_id is not None
                and self.turn.turn_id != self.interaction.turn_id
            ):
                raise ValueError("interaction mutation contains an unrelated Turn")
        if self.job is not None:
            if (
                self.job.thread_id is not None
                and self.job.thread_id != self.interaction.thread_id
            ):
                raise ValueError("interaction mutation Job belongs to another Thread")
            if (
                self.interaction.job_id is not None
                and self.job.job_id != self.interaction.job_id
            ):
                raise ValueError("interaction mutation contains an unrelated Job")
            if (
                self.job.turn_id is not None
                and self.turn is not None
                and self.job.turn_id != self.turn.turn_id
            ):
                raise ValueError("interaction mutation Job belongs to another Turn")
        return self


def _validate_connector_mutation_identity(
    mutation: InteractionMutationResponse,
    *,
    interaction_id: str,
    connector_id: str,
) -> None:
    if mutation.interaction.interaction_id != interaction_id:
        raise ValueError("connector mutation contains an unrelated Interaction")
    connector = mutation.interaction.contract.connector
    if connector is None or connector.connector_id != connector_id:
        raise ValueError("connector mutation contains an unrelated Connector")


class ConnectorLoginBeginResponse(FrozenProtocolModel):
    """Public, secret-free connector authorization challenge."""

    interaction_id: str = Field(min_length=1, max_length=256)
    connector_id: str = Field(min_length=1, max_length=256)
    state: Literal["awaiting_callback"] = "awaiting_callback"
    authorization_url: str | None = Field(default=None, min_length=8, max_length=4096)
    verification_url: str | None = Field(default=None, min_length=8, max_length=4096)
    user_code: str | None = Field(default=None, min_length=1, max_length=128)
    expires_at: datetime

    _expires_at_utc = field_validator("expires_at")(_ensure_utc)

    @model_validator(mode="after")
    def _require_public_destination(self) -> "ConnectorLoginBeginResponse":
        if self.authorization_url is None and self.verification_url is None:
            raise ValueError("connector login challenge has no public destination")
        return self


class ConnectorLoginCheckResponse(FrozenProtocolModel):
    """One exhaustive state projection for connector-login polling."""

    interaction_id: str = Field(min_length=1, max_length=256)
    connector_id: str = Field(min_length=1, max_length=256)
    connected: bool
    state: Literal[
        "awaiting_callback",
        "authorization_required",
        "reauthorization_required",
        "connected",
    ]
    reason: str | None = Field(default=None, min_length=1, max_length=128)
    authority_refresh_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    mutation: InteractionMutationResponse | None = None

    @model_validator(mode="after")
    def _validate_state_payload(self) -> "ConnectorLoginCheckResponse":
        if self.state == "connected":
            if not self.connected or self.reason is not None or self.mutation is None:
                raise ValueError("connected connector login payload is inconsistent")
            _validate_connector_mutation_identity(
                self.mutation,
                interaction_id=self.interaction_id,
                connector_id=self.connector_id,
            )
            return self
        if self.connected or self.authority_refresh_revision_id is not None:
            raise ValueError("pending connector login payload is inconsistent")
        if self.mutation is not None:
            raise ValueError("pending connector login cannot contain a mutation")
        if self.state == "awaiting_callback":
            if self.reason is not None:
                raise ValueError("awaiting connector login cannot contain a reason")
        elif self.reason is None:
            raise ValueError("retryable connector login requires a reason")
        return self


class ConnectorLoginCancelResponse(FrozenProtocolModel):
    interaction_id: str = Field(min_length=1, max_length=256)
    connector_id: str = Field(min_length=1, max_length=256)
    cancelled: Literal[True] = True
    mutation: InteractionMutationResponse

    @model_validator(mode="after")
    def _validate_mutation_identity(self) -> "ConnectorLoginCancelResponse":
        _validate_connector_mutation_identity(
            self.mutation,
            interaction_id=self.interaction_id,
            connector_id=self.connector_id,
        )
        return self


class InteractionListResponse(ProtocolModel):
    interactions: list[InteractionProjection]
    watermark: int = Field(ge=0)


class CreateThreadRequest(ProtocolModel):
    title: str | None = Field(default=None, max_length=200)
    metadata: JsonObject = Field(default_factory=dict)
    client_request_id: str | None = None


class CreateTurnRequest(ProtocolModel):
    input: str = Field(min_length=1)
    agent_model_id: str = Field(
        default="ecorex-chat", min_length=1, max_length=256
    )
    image_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    # Explicit user/tool-menu selection is structured.  Runtime may still
    # recognize conservative "use <alias>" prose for compatibility, but a bare
    # mention never receives explicit-selection authority.
    explicit_tool_ids: list[str] = Field(default_factory=list, max_length=64)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    client_message_id: str | None = None
    metadata: JsonObject = Field(default_factory=dict)


class SteerTurnRequest(ProtocolModel):
    input: str = Field(min_length=1)
    # Steer inherits the active Turn models. Optional values are optimistic
    # expectations only; changing models requires queue/replace.
    agent_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    image_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    explicit_tool_ids: list[str] = Field(default_factory=list, max_length=64)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    client_message_id: str | None = None
    metadata: JsonObject = Field(default_factory=dict)


class QueueTurnRequest(CreateTurnRequest):
    pass


class ReplaceTurnRequest(CreateTurnRequest):
    reason: str = "replaced_by_user"


class ForkThreadRequest(ProtocolModel):
    from_turn_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    metadata: JsonObject = Field(default_factory=dict)
    client_request_id: str | None = None


class InterruptTurnRequest(ProtocolModel):
    reason: str = "interrupted_by_user"


class TurnMutationResponse(ProtocolModel):
    turn: TurnProjection
    job: JobProjection | None = None
    watermark: int


class ReplaceTurnResponse(ProtocolModel):
    superseded_turn: TurnProjection
    replacement_turn: TurnProjection
    job: JobProjection
    watermark: int


class ThreadProjectionResponse(ProtocolModel):
    thread: ThreadProjection
    turns: list[TurnProjection]
    items: list[ItemProjection]
    jobs: list[JobProjection]
    interactions: list[InteractionProjection]
    watermark: int


class ThreadListResponse(FrozenProtocolModel):
    items: list[ThreadProjection]
    next_cursor: str | None = None


class RenameThreadRequest(ProtocolModel):
    title: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=256)


class ThreadStatusRequest(ProtocolModel):
    client_request_id: str = Field(min_length=1, max_length=256)


class ThreadPinRequest(ProtocolModel):
    pinned: bool
    client_request_id: str = Field(min_length=1, max_length=256)


class EventListResponse(ProtocolModel):
    events: list[EventEnvelope]
    after_seq: int = Field(ge=0)
    watermark: int = Field(ge=0)
    has_more: bool


class ReplayInteractionProjection(InteractionProjection):
    """Event-derived interaction projection used by side-effect-free Replay."""


class MockReplayResponse(FrozenProtocolModel):
    projection: ThreadProjectionResponse
    interactions: list[ReplayInteractionProjection] = Field(default_factory=list)
    live_replay_turn_ids: list[str] = Field(default_factory=list)
    source_watermark: int = Field(ge=1)
    through_seq: int = Field(ge=1)
    event_count: int = Field(ge=1)
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveReplayRequest(ProtocolModel):
    source_turn_id: str = Field(min_length=1, max_length=256)
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=1, max_length=256)


class LiveReplayResponse(FrozenProtocolModel):
    source_thread_id: str
    source_turn_id: str
    causation_event_id: str
    replay: TurnMutationResponse
    permission_snapshot_id: str
    extension_snapshot_id: str


class TraceSpanProjection(FrozenProtocolModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    kind: Literal["INTERNAL", "CLIENT", "SERVER"] = "INTERNAL"
    start_time_unix_nano: str = Field(pattern=r"^[0-9]+$")
    end_time_unix_nano: str = Field(pattern=r"^[0-9]+$")
    status: Literal["UNSET", "OK", "ERROR"] = "UNSET"
    attributes: JsonObject = Field(default_factory=dict)
    events: list[JsonObject] = Field(default_factory=list)


class TraceProjectionResponse(FrozenProtocolModel):
    thread_id: str
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    through_seq: int = Field(ge=1)
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    spans: list[TraceSpanProjection]
    otlp: JsonObject


class AuditRecordProjection(FrozenProtocolModel):
    audit_id: str
    source_event_id: str
    category: Literal[
        "prompt",
        "response",
        "tool",
        "permission",
        "task",
        "artifact",
        "human",
        "connector",
    ]
    event_type: str
    account_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    payload: JsonObject = Field(default_factory=dict)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary_included: Literal[False] = False
    delivery_status: Literal["pending", "retry_wait", "published", "rejected"]
    attempts: int = Field(ge=0)
    next_attempt_at: datetime | None = None
    created_at: datetime
    published_at: datetime | None = None
    rejected_at: datetime | None = None
    last_error_code: str | None = None

    _timestamps_utc = field_validator(
        "next_attempt_at", "created_at", "published_at", "rejected_at"
    )(_ensure_utc)


class AuditListResponse(FrozenProtocolModel):
    records: list[AuditRecordProjection]
    count: int = Field(ge=0)


class AuditDrainRequest(ProtocolModel):
    limit: int = Field(default=100, ge=1, le=1000)


class AuditDrainResponse(FrozenProtocolModel):
    attempted: int = Field(ge=0)
    published: int = Field(ge=0)
    retry_scheduled: int = Field(ge=0)
    rejected: int = Field(default=0, ge=0)
    pending: int = Field(ge=0)


class AuditRetentionResponse(FrozenProtocolModel):
    raw_deleted: int = Field(ge=0)
    aggregate_deleted: int = Field(ge=0)


class LoginSnapshot(FrozenProtocolModel):
    authenticated: bool
    account_id: str | None = None
    display_name: str | None = None
    organization_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    session_revision: int | None = Field(default=None, ge=1, strict=True)
    session_lease_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class PolicyLeaseSnapshot(FrozenProtocolModel):
    lease_id: str
    issued_at: datetime
    expires_at: datetime
    duration_hours: int = Field(default=72, ge=1, le=72, strict=True)

    _timestamps_utc = field_validator("issued_at", "expires_at")(_ensure_utc)

    @model_validator(mode="after")
    def _bounded_duration(self) -> "PolicyLeaseSnapshot":
        duration = self.expires_at - self.issued_at
        if duration.total_seconds() <= 0 or duration > timedelta(hours=72):
            raise ValueError("policy lease duration must be within 72 hours")
        return self


class ModelContextManagementDescriptor(FrozenProtocolModel):
    type: Literal["compaction"]
    compact_threshold_tokens: int = Field(ge=1_000, le=2_000_000, strict=True)


class ModelPolicyDescriptor(FrozenProtocolModel):
    schema_version: Literal[1]
    policy_id: str
    policy_version: str
    local_model_id: str
    upstream_model_id: str
    reasoning_effort: Literal["medium", "high"]
    context_management: ModelContextManagementDescriptor


class ModelDescriptor(FrozenProtocolModel):
    model_id: str
    display_name: str
    capabilities: list[str]
    aliases: list[str] = Field(default_factory=list)
    is_default: bool = False
    model_policy: ModelPolicyDescriptor | None = None


class ModelCatalog(FrozenProtocolModel):
    snapshot_id: str | None = None
    chat: list[ModelDescriptor] = Field(default_factory=list)
    image: list[ModelDescriptor] = Field(default_factory=list)
    vision: list[ModelDescriptor] = Field(default_factory=list)
    audio: list[ModelDescriptor] = Field(default_factory=list)
    embedding: list[ModelDescriptor] = Field(default_factory=list)


class QuotaSnapshot(FrozenProtocolModel):
    remaining: int | None = None
    unit: str = "requests"
    resets_at: datetime | None = None
    limits: dict[str, int] = Field(default_factory=dict)

    _resets_at_utc = field_validator("resets_at")(_ensure_utc)


class TokenUsageWindow(FrozenProtocolModel):
    """Provider-reported token totals for one calendar window.

    The Runtime deliberately does not estimate token counts in the WebUI. A
    zero means that no completed model response reported usage in the window;
    it is not a proxy for a configured quota.
    """

    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    total_tokens: int = Field(default=0, ge=0, strict=True)


class ContextUsageProjection(FrozenProtocolModel):
    """Latest provider-reported context and the selected model threshold."""

    used_tokens: int | None = Field(default=None, ge=0, strict=True)
    window_tokens: int | None = Field(default=None, ge=1_000, strict=True)
    model_id: str | None = None
    model_display_name: str | None = None
    model_catalog_snapshot_id: str | None = None
    measured_at: datetime | None = None

    _measured_at_utc = field_validator("measured_at")(_ensure_utc)


class TaskActivityDay(FrozenProtocolModel):
    """Terminal Turn counts for one day in the configured Runtime timezone."""

    date: date
    completed: int = Field(default=0, ge=0, strict=True)
    terminal: int = Field(default=0, ge=0, strict=True)


class TaskActivityProjection(FrozenProtocolModel):
    """Device-local task activity derived from authoritative Turn states."""

    completed_today: int = Field(default=0, ge=0, strict=True)
    waiting: int = Field(default=0, ge=0, strict=True)
    terminal_today: int = Field(default=0, ge=0, strict=True)
    days: list[TaskActivityDay] = Field(default_factory=list, max_length=7)


class ConversationUsageProjection(FrozenProtocolModel):
    """Read-only usage projection for the active conversation composer."""

    thread_id: str = Field(min_length=1)
    timezone: str = Field(min_length=1, max_length=64)
    scope: Literal["account", "local_device"] = "local_device"
    source: Literal["managed_gateway", "local_event_store"] = "local_event_store"
    complete_across_devices: bool = False
    today: TokenUsageWindow = Field(default_factory=TokenUsageWindow)
    week: TokenUsageWindow = Field(default_factory=TokenUsageWindow)
    context: ContextUsageProjection = Field(default_factory=ContextUsageProjection)
    task_activity: TaskActivityProjection = Field(default_factory=TaskActivityProjection)
    calculated_at: datetime = Field(default_factory=utc_now)

    _calculated_at_utc = field_validator("calculated_at")(_ensure_utc)


class ModelServiceSnapshot(FrozenProtocolModel):
    state: Literal["ready", "unavailable"]
    reason: str | None = None


class LogoutSessionRequest(ProtocolModel):
    lease_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=8, max_length=256)
    confirmed: Literal[True]


class LogoutSessionResponse(FrozenProtocolModel):
    authenticated: Literal[False] = False
    generation: int = Field(ge=1, strict=True)
    restart_required: Literal[True] = True
    restart_scheduled: bool = False


class PasswordSessionChangeRequest(ProtocolModel):
    schema_version: Literal[1]
    current_password: SecretStr = Field(min_length=8, max_length=256)
    new_password: SecretStr = Field(min_length=10, max_length=256)
    client_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$",
    )


class PasswordSessionChangeResponse(FrozenProtocolModel):
    schema_version: Literal[1] = 1
    status: Literal["changed"] = "changed"
    reauthentication_required: Literal[True] = True


class PasswordSessionLoginRequest(ProtocolModel):
    identifier: str = Field(min_length=1, max_length=254)
    # Login remains compatible with imported v0.2.9.2 credentials. Creating
    # or resetting a credential is governed separately by the 10-char admin
    # contract.
    password: SecretStr = Field(min_length=8, max_length=256)
    client_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$",
    )

    @field_validator("identifier")
    @classmethod
    def _identifier(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or "\x00" in normalized
            or any(ord(character) < 33 for character in normalized)
        ):
            raise ValueError("login identifier is invalid")
        return normalized


class PasswordSessionLoginResponse(FrozenProtocolModel):
    authenticated: Literal[True] = True
    display_name: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, strict=True)
    restart_required: Literal[True] = True
    restart_scheduled: bool = False


class StartDeviceLoginRequest(ProtocolModel):
    client_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$",
    )


class PollDeviceLoginRequest(ProtocolModel):
    client_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$",
    )


class DeviceLoginProjection(FrozenProtocolModel):
    flow_id: str = Field(pattern=r"^devflow_[0-9a-f]{32}$")
    status: Literal["pending", "authorized", "denied", "expired", "failed"]
    user_code: str = Field(min_length=4, max_length=64)
    verification_url: str = Field(min_length=8, max_length=4096)
    expires_at: datetime
    poll_interval_seconds: int = Field(ge=1, le=300, strict=True)
    next_poll_at: datetime
    restart_required: bool = False
    restart_scheduled: bool = False
    session_generation: int | None = Field(default=None, ge=1, strict=True)
    error_code: str | None = Field(default=None, max_length=128)

    _device_times_utc = field_validator("expires_at", "next_poll_at")(_ensure_utc)


class PermissionSnapshot(FrozenProtocolModel):
    snapshot_id: str = Field(pattern=r"^perm_[0-9a-f]{64}$")
    profile: Literal["default", "full_access"]
    revision: int = Field(ge=1, strict=True)
    updated_at: datetime
    sandbox: Literal["workspace-write", "danger-full-access"]
    approval: Literal["on-request", "never"]
    full_access: bool
    admin_hard_denies: tuple[str, ...] = ()

    _updated_at_utc = field_validator("updated_at")(_ensure_utc)

    @field_validator("admin_hard_denies")
    @classmethod
    def _canonical_hard_denies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical: list[str] = []
        for value in values:
            normalized = value.strip().casefold()
            if not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", normalized):
                raise ValueError("administrator hard-deny capability ID is invalid")
            canonical.append(normalized)
        if canonical != sorted(set(canonical)):
            raise ValueError("administrator hard-deny capability IDs must be sorted and unique")
        return tuple(canonical)

    @model_validator(mode="after")
    def _consistent_permission_fact(self) -> "PermissionSnapshot":
        full_access = self.profile == "full_access"
        expected_sandbox = "danger-full-access" if full_access else "workspace-write"
        expected_approval = "never" if full_access else "on-request"
        if self.full_access is not full_access:
            raise ValueError("permission profile and full_access disagree")
        if self.sandbox != expected_sandbox or self.approval != expected_approval:
            raise ValueError("permission profile, sandbox, and approval disagree")
        if self.snapshot_id != self.expected_snapshot_id(
            profile=self.profile,
            revision=self.revision,
            updated_at=self.updated_at,
            admin_hard_denies=self.admin_hard_denies,
        ):
            raise ValueError("permission snapshot digest is invalid")
        return self

    @classmethod
    def issue(
        cls,
        *,
        profile: Literal["default", "full_access"],
        revision: int,
        updated_at: datetime,
        admin_hard_denies: Sequence[str],
    ) -> "PermissionSnapshot":
        full_access = profile == "full_access"
        canonical_denies = sorted(
            {value.strip().casefold() for value in admin_hard_denies if value.strip()}
        )
        return cls(
            snapshot_id=cls.expected_snapshot_id(
                profile=profile,
                revision=revision,
                updated_at=updated_at,
                admin_hard_denies=canonical_denies,
            ),
            profile=profile,
            revision=revision,
            updated_at=updated_at,
            sandbox="danger-full-access" if full_access else "workspace-write",
            approval="never" if full_access else "on-request",
            full_access=full_access,
            admin_hard_denies=canonical_denies,
        )

    @staticmethod
    def expected_snapshot_id(
        *,
        profile: str,
        revision: int,
        updated_at: datetime,
        admin_hard_denies: Sequence[str],
    ) -> str:
        normalized_time = _ensure_utc(updated_at)
        if normalized_time is None:
            raise ValueError("permission updated_at is required")
        payload = {
            "profile": profile,
            "revision": revision,
            "updated_at": normalized_time.isoformat(),
            "sandbox": (
                "danger-full-access" if profile == "full_access" else "workspace-write"
            ),
            "approval": "never" if profile == "full_access" else "on-request",
            "full_access": profile == "full_access",
            "admin_hard_denies": list(admin_hard_denies),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "perm_" + hashlib.sha256(encoded).hexdigest()


class UpdatePermissionRequest(ProtocolModel):
    profile: Literal["default", "full_access"]
    expected_revision: int = Field(ge=1, strict=True)
    client_request_id: str = Field(min_length=1, max_length=256)


class PermissionMutationResponse(FrozenProtocolModel):
    permissions: PermissionSnapshot


class ConnectorDescriptor(FrozenProtocolModel):
    connector_id: str
    display_name: str
    tier: Literal["stable", "beta"]
    health: Literal["connected", "disconnected", "degraded", "unconfigured"]
    capabilities: list[str] = Field(default_factory=list)
    contract_version: str = "1.0"
    description: str | None = None
    auth_kinds: list[str] = Field(default_factory=list)
    icon_key: str | None = None
    adapter_available: bool = False
    unavailable_reason: str | None = None


class ExtensionDependencyProjection(FrozenProtocolModel):
    extension_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version_range: str = Field(min_length=1, max_length=256)


class ExtensionExportProjection(FrozenProtocolModel):
    export_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,127}$")
    kind: Literal["tool", "skill", "mcp_server", "connector", "capability_pack"]
    exposure: Literal["direct", "deferred", "hidden"]
    permission_effects: list[
        Literal["read", "write", "network", "execute", "ui_automation", "generate_media", "subscribe"]
    ] = Field(default_factory=list)


class ExtensionActionProjection(FrozenProtocolModel):
    action_id: Literal["enable", "disable", "health_check", "rollback", "configure", "uninstall"]
    enabled: bool
    disabled_reason: str | None = None
    requires_confirmation: bool


class ExtensionProjection(FrozenProtocolModel):
    extension_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2048)
    kind: Literal[
        "skill", "mcp_server", "tool_provider", "connector_provider", "capability_pack"
    ]
    category: Literal[
        "system",
        "office",
        "image_media",
        "collaboration",
        "data",
        "development",
        "automation",
        "general",
    ]
    icon_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    active_revision_id: str | None = Field(
        default=None, pattern=r"^extrev_[0-9a-f]{64}$"
    )
    active_version: str | None = None
    active_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: Literal[
        "core_bundle",
        "signed_release",
        "capability_pack",
        "administrator",
        "local_bundle",
        "legacy_import",
    ]
    trust: Literal["builtin", "administrator", "verified_publisher", "local_untrusted"]
    status: Literal["staged", "enabled", "disabled", "quarantined", "uninstalled"]
    health: Literal["unknown", "healthy", "degraded", "unhealthy", "circuit_open"]
    provenance: dict[str, str | None]
    readiness: Literal["ready", "needs_configuration", "missing_runtime", "unsupported"]
    requirements: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    dependencies: list[ExtensionDependencyProjection] = Field(default_factory=list)
    exports: list[ExtensionExportProjection] = Field(default_factory=list)
    actions: list[ExtensionActionProjection] = Field(default_factory=list)
    last_error_code: str | None = None
    revision: int = Field(ge=1, strict=True)
    updated_at: datetime

    _extension_updated_at_utc = field_validator("updated_at")(_ensure_utc)


class ExtensionCatalogSnapshot(FrozenProtocolModel):
    snapshot_id: str = Field(pattern=r"^ext_[0-9a-f]{64}$")
    contract_version: Literal["1.0"] = "1.0"
    extension_generation: int = Field(ge=0, strict=True)
    items: list[ExtensionProjection] = Field(default_factory=list)


class SkillProvenance(FrozenProtocolModel):
    brand: Literal["e-Mate"] = "e-Mate"
    original_platform: str | None = Field(default=None, max_length=64)
    original_url: str | None = Field(default=None, max_length=2048)


class SkillHubUploaderProjection(FrozenProtocolModel):
    nickname: str = Field(min_length=1, max_length=64)
    author_ref: str = Field(pattern=r"^author_[0-9a-f]{24}$")


class SkillHubCardProjection(FrozenProtocolModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,95}$")
    title: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2048)
    version: str = Field(min_length=5, max_length=64)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_size_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    tags: list[str] = Field(default_factory=list, max_length=32)
    category: Literal["third_party", "content_creation", "office_productivity"]
    uploader: SkillHubUploaderProjection
    provenance: SkillProvenance
    installation_status: Literal[
        "not_installed", "installed_enabled", "installed_disabled", "uninstalled"
    ] = "not_installed"
    readiness: Literal["ready", "needs_configuration", "missing_runtime", "unsupported"] = "ready"


class SkillHubListResponse(FrozenProtocolModel):
    schema_version: Literal[1] = 1
    items: list[SkillHubCardProjection] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, max_length=96)


class SkillHubDetailProjection(FrozenProtocolModel):
    schema_version: Literal[1] = 1
    skill: SkillHubCardProjection
    versions: list[SkillHubCardProjection] = Field(min_length=1, max_length=100)


class ExtensionMutationResponse(FrozenProtocolModel):
    extension: ExtensionProjection
    extensions: ExtensionCatalogSnapshot


def _empty_extension_catalog() -> ExtensionCatalogSnapshot:
    payload = json.dumps(
        {"contract_version": "1.0", "extension_generation": 0, "items": []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ExtensionCatalogSnapshot(
        snapshot_id="ext_" + hashlib.sha256(payload).hexdigest(),
        extension_generation=0,
        items=[],
    )


class UpdateSnapshot(FrozenProtocolModel):
    current_version: str
    state: Literal[
        "idle",
        "available",
        "downloading",
        "awaiting_user",
        "activating",
        "failed",
    ] = "idle"
    target_version: str | None = None
    release_id: str | None = None
    build_digest: str | None = None
    transaction_id: str | None = None
    can_activate: bool = False
    requires_refresh: bool = False
    error_code: str | None = None

    @model_validator(mode="after")
    def _consistent_update_state(self) -> "UpdateSnapshot":
        identity = (self.target_version, self.release_id, self.build_digest)
        if self.state == "idle" and any(
            value is not None for value in (*identity, self.transaction_id, self.error_code)
        ):
            raise ValueError("idle update state cannot contain a target")
        if self.state in {"available", "downloading", "awaiting_user", "activating"}:
            if any(not value for value in identity):
                raise ValueError("active update state requires a release identity")
        if self.state in {"awaiting_user", "activating"} and not self.transaction_id:
            raise ValueError("prepared update state requires a transaction")
        if self.build_digest is not None and (
            len(self.build_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.build_digest)
        ):
            raise ValueError("update build digest must be lowercase SHA-256")
        if self.can_activate and (
            self.state != "awaiting_user" or not self.transaction_id
        ):
            raise ValueError("only an awaiting update can be activated")
        if self.requires_refresh and self.state != "activating":
            raise ValueError("only an activated update can require refresh")
        if self.error_code is not None and self.state != "failed":
            raise ValueError("only a failed update can expose an error code")
        return self


class CheckUpdateResponse(FrozenProtocolModel):
    update: UpdateSnapshot


class ActivateUpdateRequest(ProtocolModel):
    transaction_id: str = Field(min_length=1, max_length=128)
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=1, max_length=256)


class ActivateUpdateResponse(FrozenProtocolModel):
    update: UpdateSnapshot
    restart_scheduled: bool
    reload_after_ms: int = Field(default=800, ge=0, le=30_000)


class BootstrapResponse(FrozenProtocolModel):
    api_version: Literal["v1"] = "v1"
    event_schema_version: Literal[1] = 1
    storage_schema_version: Literal[1] = 1
    login: LoginSnapshot
    policy_lease: PolicyLeaseSnapshot | None
    models: ModelCatalog
    model_service: ModelServiceSnapshot
    login_service: ModelServiceSnapshot = ModelServiceSnapshot(
        state="unavailable", reason="device_authorization_not_configured"
    )
    share_service: ModelServiceSnapshot = ModelServiceSnapshot(
        state="unavailable", reason="share_service_not_configured"
    )
    retouch_service: ModelServiceSnapshot = ModelServiceSnapshot(
        state="unavailable", reason="managed_image_edit_not_configured"
    )
    quota: QuotaSnapshot
    permissions: PermissionSnapshot
    connectors: list[ConnectorDescriptor]
    extensions: ExtensionCatalogSnapshot = Field(default_factory=_empty_extension_catalog)
    update: UpdateSnapshot
    csrf_token: str
    server_time: datetime = Field(default_factory=utc_now)

    _server_time_utc = field_validator("server_time")(_ensure_utc)

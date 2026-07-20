"""Versioned local contracts for the EcoreX managed Model Gateway."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ecorex.managed_model_policy import (
    ECOREX_CHAT_MODEL_POLICY,
    ManagedChatModelPolicy,
    managed_chat_model_policy,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_JSON_DEPTH = 20
_MAX_JSON_NODES = 50_000
_MAX_JSON_STRING = 1_000_000

# The model-facing tool working set is deliberately much smaller than the
# searchable capability catalog.  These are protocol limits, not tuning
# hints: both the local Runtime request model and the cloud provider adapter
# enforce them before a model request can cross the network boundary.
TOOL_PROJECTION_BUDGET_VERSION = "1.0.0"
MAX_MODEL_VISIBLE_TOOLS = 16
MAX_DISCLOSED_WORKING_SET = 12
MAX_TOOL_DESCRIPTOR_BYTES = 96 * 1024
MAX_TOOL_SCHEMA_BATCH_BYTES = 256 * 1024


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_id(value: str | None, label: str) -> None:
    if value is not None and _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} contains an unsafe identifier")


def _validate_json_value(value: Any, label: str) -> None:
    remaining = _MAX_JSON_NODES
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        remaining -= 1
        if remaining < 0:
            raise ValueError(f"{label} contains too many values")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{label} is nested too deeply")
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if current != current or current in {float("inf"), float("-inf")}:
                raise ValueError(f"{label} contains a non-finite number")
            continue
        if isinstance(current, str):
            if len(current) > _MAX_JSON_STRING:
                raise ValueError(f"{label} contains an oversized string")
            if _has_surrogate(current):
                raise ValueError(f"{label} contains invalid Unicode")
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if (
                    not isinstance(key, str)
                    or len(key) > 256
                    or _has_surrogate(key)
                    or any(ord(character) < 32 for character in key)
                ):
                    raise ValueError(f"{label} contains an unsafe object key")
                pending.append((item, depth + 1))
            continue
        raise ValueError(f"{label} contains a non-JSON value")


def canonical_tool_descriptor_bytes(value: Any) -> bytes:
    """Return the exact canonical UTF-8 representation used for budgeting."""

    _validate_json_value(value, "managed tool descriptor")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("managed tool descriptor is not canonical JSON") from None


def canonical_tool_schema_batch_bytes(values: list[dict[str, Any]]) -> bytes:
    """Return canonical bytes for one complete model-visible descriptor batch."""

    if not values:
        return b""
    _validate_json_value(values, "direct tool catalog")
    try:
        return json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("direct tool catalog is not canonical JSON") from None


def validate_tool_projection_budget(
    descriptors: list[dict[str, Any]],
    disclosed_tool_ids: list[str],
) -> int:
    """Validate the bounded model-visible working set and return its byte size."""

    if len(descriptors) > MAX_MODEL_VISIBLE_TOOLS:
        raise ValueError("model-visible tool count exceeds the product budget")
    if len(disclosed_tool_ids) > MAX_DISCLOSED_WORKING_SET:
        raise ValueError("disclosed tool working set exceeds the product budget")
    projected_ids: list[str] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ValueError("managed tool descriptor is invalid")
        spec = descriptor.get("spec")
        tool_id = spec.get("tool_id") if isinstance(spec, dict) else None
        if not isinstance(tool_id, str):
            raise ValueError("managed tool descriptor identity is invalid")
        projected_ids.append(tool_id)
        if len(canonical_tool_descriptor_bytes(descriptor)) > MAX_TOOL_DESCRIPTOR_BYTES:
            raise ValueError("managed tool descriptor exceeds the product byte budget")
    if len(projected_ids) != len(set(projected_ids)):
        raise ValueError("model-visible tool IDs must be unique")
    if not set(disclosed_tool_ids) <= set(projected_ids):
        raise ValueError("disclosed tool projection is incomplete")
    total = len(canonical_tool_schema_batch_bytes(descriptors))
    if total > MAX_TOOL_SCHEMA_BATCH_BYTES:
        raise ValueError("managed tool catalog exceeds the product byte budget")
    return total


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GatewayEventType(StrEnum):
    OUTPUT_TEXT_DELTA = "output_text.delta"
    REASONING_SUMMARY_DELTA = "reasoning_summary.delta"
    TOOL_CALL_REQUESTED = "tool_call.requested"
    RESPONSE_COMPLETED = "response.completed"
    RESPONSE_FAILED = "response.failed"


class GatewayTokenUsageWindow(GatewayModel):
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    total_tokens: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_total(self) -> "GatewayTokenUsageWindow":
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("gateway token total is inconsistent")
        return self


class GatewayAccountUsageProjection(GatewayModel):
    """Provider-reported usage for the authenticated account across devices."""

    schema_version: Literal[1] = 1
    scope: Literal["account"] = "account"
    timezone: str = Field(min_length=1, max_length=64)
    today: GatewayTokenUsageWindow = Field(default_factory=GatewayTokenUsageWindow)
    week: GatewayTokenUsageWindow = Field(default_factory=GatewayTokenUsageWindow)
    week_started_at: datetime
    coverage_started_at: datetime | None = None
    calculated_at: datetime

    @model_validator(mode="after")
    def validate_projection(self) -> "GatewayAccountUsageProjection":
        if (
            self.calculated_at.tzinfo is None
            or self.week_started_at.tzinfo is None
            or (
                self.coverage_started_at is not None
                and self.coverage_started_at.tzinfo is None
            )
        ):
            raise ValueError("gateway usage timestamp must be timezone-aware")
        if self.week_started_at > self.calculated_at:
            raise ValueError("gateway usage window is invalid")
        return self


class GatewayToolOutput(GatewayModel):
    tool_call_id: str = Field(min_length=1, max_length=256)
    output: Any

    @model_validator(mode="after")
    def validate_output(self) -> "GatewayToolOutput":
        _validate_id(self.tool_call_id, "tool_call_id")
        _validate_json_value(self.output, "tool output")
        return self


class GatewayUserMessageInput(GatewayModel):
    """One stable user-authored revision in a model round.

    ``message_id`` is an EcoreX identity used for deduplication and audit.  It
    is deliberately not projected as a provider-controlled message ID.
    """

    type: Literal["user_message"] = "user_message"
    message_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_message(self) -> "GatewayUserMessageInput":
        _validate_id(self.message_id, "message_id")
        _validate_json_value(self.content, "user message")
        return self


class GatewayAssistantMessageInput(GatewayModel):
    """One completed assistant message replayed from durable Thread history.

    The Runtime owns ``message_id`` and only reconstructs this input from
    completed public message Items.  Keeping it distinct from user input
    preserves normal dialogue roles for a follow-up such as ``5``.
    """

    type: Literal["assistant_message"] = "assistant_message"
    message_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_message(self) -> "GatewayAssistantMessageInput":
        _validate_id(self.message_id, "message_id")
        _validate_json_value(self.content, "assistant message")
        return self


class GatewayFunctionCallOutputInput(GatewayModel):
    """One result returned to a function call from the previous response."""

    type: Literal["function_call_output"] = "function_call_output"
    tool_call_id: str = Field(min_length=1, max_length=256)
    output: Any

    @model_validator(mode="after")
    def validate_output(self) -> "GatewayFunctionCallOutputInput":
        _validate_id(self.tool_call_id, "tool_call_id")
        _validate_json_value(self.output, "tool output")
        return self


GatewayInputItem = Annotated[
    (
        GatewayUserMessageInput
        | GatewayAssistantMessageInput
        | GatewayFunctionCallOutputInput
    ),
    Field(discriminator="type"),
]


class GatewayContextManagementPolicy(GatewayModel):
    """Provider context policy frozen into every managed model request."""

    type: Literal["compaction"] = "compaction"
    compact_threshold_tokens: int = Field(ge=1_000, le=2_000_000, strict=True)


class GatewayModelPolicy(GatewayModel):
    """Auditable model policy; callers cannot override the Gateway authority."""

    schema_version: Literal[1] = 1
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(
        min_length=5,
        max_length=32,
        pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
    )
    local_model_id: str = Field(min_length=1, max_length=128)
    upstream_model_id: str = Field(min_length=1, max_length=128)
    reasoning_effort: Literal["medium"] = "medium"
    context_management: GatewayContextManagementPolicy

    @model_validator(mode="after")
    def validate_identity(self) -> "GatewayModelPolicy":
        for label, value in (
            ("policy_id", self.policy_id),
            ("local_model_id", self.local_model_id),
            ("upstream_model_id", self.upstream_model_id),
        ):
            _validate_id(value, label)
        return self


def gateway_model_policy(policy: ManagedChatModelPolicy) -> GatewayModelPolicy:
    return GatewayModelPolicy(
        schema_version=policy.schema_version,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        local_model_id=policy.local_model_id,
        upstream_model_id=policy.upstream_model_id,
        reasoning_effort=policy.reasoning_effort,
        context_management=GatewayContextManagementPolicy(
            type=policy.context_management_type,
            compact_threshold_tokens=policy.compact_threshold_tokens,
        ),
    )


def ecorex_chat_gateway_policy(
    local_model_id: str = ECOREX_CHAT_MODEL_POLICY.local_model_id,
) -> GatewayModelPolicy:
    return gateway_model_policy(managed_chat_model_policy(local_model_id))


class ModelGatewayRequest(GatewayModel):
    schema_version: Literal[1] = 1
    tool_projection_budget_version: Literal["1.0.0"] = (
        TOOL_PROJECTION_BUDGET_VERSION
    )
    request_id: str = Field(min_length=1, max_length=256)
    thread_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    trace_id: str = Field(min_length=1, max_length=256)
    model_id: str = Field(min_length=1, max_length=256)
    model_policy: GatewayModelPolicy = Field(
        default_factory=ecorex_chat_gateway_policy
    )
    # ``input`` and ``tool_outputs`` are the v1 compatibility surface.  New
    # callers use ``input_items`` so a tool continuation and pending user
    # revisions can coexist without either side being discarded.
    input: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    input_items: list[GatewayInputItem] | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    config_snapshot_id: str = Field(min_length=1, max_length=256)
    capability_snapshot_id: str = Field(min_length=1, max_length=256)
    permission_snapshot_id: str = Field(min_length=1, max_length=256)
    direct_tools: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_MODEL_VISIBLE_TOOLS,
    )
    deferred_tool_ids: list[str] = Field(default_factory=list, max_length=1024)
    disclosed_tool_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_DISCLOSED_WORKING_SET,
    )
    suppressed_tool_ids: list[str] = Field(default_factory=list, max_length=128)
    previous_response_id: str | None = Field(default=None, max_length=256)
    tool_outputs: list[GatewayToolOutput] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_resources(self) -> "ModelGatewayRequest":
        if self.input is not None:
            _validate_json_value(self.input, "model input")
        for label, value in (
            ("request_id", self.request_id),
            ("thread_id", self.thread_id),
            ("turn_id", self.turn_id),
            ("trace_id", self.trace_id),
            ("model_id", self.model_id),
            ("config_snapshot_id", self.config_snapshot_id),
            ("capability_snapshot_id", self.capability_snapshot_id),
            ("permission_snapshot_id", self.permission_snapshot_id),
            ("previous_response_id", self.previous_response_id),
        ):
            _validate_id(value, label)
        for tool_id in self.deferred_tool_ids:
            _validate_id(tool_id, "deferred tool id")
        for tool_id in self.disclosed_tool_ids:
            _validate_id(tool_id, "disclosed tool id")
        for tool_id in self.suppressed_tool_ids:
            _validate_id(tool_id, "suppressed tool id")
        if len(self.deferred_tool_ids) != len(set(self.deferred_tool_ids)):
            raise ValueError("deferred tool IDs must be unique")
        if len(self.disclosed_tool_ids) != len(set(self.disclosed_tool_ids)):
            raise ValueError("disclosed tool IDs must be unique")
        if len(self.suppressed_tool_ids) != len(set(self.suppressed_tool_ids)):
            raise ValueError("suppressed tool IDs must be unique")
        if set(self.deferred_tool_ids) & set(self.disclosed_tool_ids):
            raise ValueError("deferred and disclosed tool IDs must be disjoint")
        if set(self.suppressed_tool_ids) & set(self.disclosed_tool_ids):
            raise ValueError("suppressed and disclosed tool IDs must be disjoint")
        if not set(self.suppressed_tool_ids) <= set(self.deferred_tool_ids):
            raise ValueError("suppressed tool IDs must remain deferred")
        output_ids = [output.tool_call_id for output in self.tool_outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("tool output IDs must be unique")
        if self.tool_outputs and self.previous_response_id is None:
            raise ValueError("tool outputs require a previous response")
        if self.input_items is not None:
            if self.input is not None or self.tool_outputs:
                raise ValueError(
                    "typed input items cannot be combined with compatibility input fields"
                )
            message_ids: list[str] = []
            typed_output_ids: list[str] = []
            message_seen = False
            total_message_characters = 0
            for item in self.input_items:
                if isinstance(
                    item, (GatewayUserMessageInput, GatewayAssistantMessageInput)
                ):
                    message_seen = True
                    message_ids.append(item.message_id)
                    total_message_characters += len(item.content)
                else:
                    if message_seen:
                        raise ValueError(
                            "function call outputs must precede conversation messages"
                        )
                    typed_output_ids.append(item.tool_call_id)
            if len(message_ids) != len(set(message_ids)):
                raise ValueError("user message IDs must be unique")
            if len(typed_output_ids) != len(set(typed_output_ids)):
                raise ValueError("tool output IDs must be unique")
            if set(message_ids) & set(typed_output_ids):
                raise ValueError("gateway input item IDs must be unique")
            if len(typed_output_ids) > 128:
                raise ValueError("too many function call outputs")
            if total_message_characters > 1_000_000:
                raise ValueError("conversation message input is oversized")
            if typed_output_ids and self.previous_response_id is None:
                raise ValueError("tool outputs require a previous response")
            _validate_json_value(
                [item.model_dump(mode="json") for item in self.input_items],
                "typed model input",
            )
        elif self.input is None and not self.tool_outputs:
            raise ValueError("model input is required")
        _validate_json_value(self.direct_tools, "direct tool catalog")
        validate_tool_projection_budget(
            self.direct_tools,
            self.disclosed_tool_ids,
        )
        _validate_json_value(
            [output.model_dump(mode="json") for output in self.tool_outputs],
            "tool outputs",
        )
        return self

    def ordered_input_items(self) -> tuple[GatewayInputItem, ...]:
        """Return the authoritative, ordered v2 input for provider projection.

        Compatibility conversion intentionally preserves v1 behavior: a
        continuation carrying ``tool_outputs`` does not resend the legacy
        ``input`` string, because existing Runtime callers retain the original
        Turn input in that field on every model round.  A caller that needs to
        combine outputs with a new user steer must use ``input_items``.
        """

        if self.input_items is not None:
            return tuple(self.input_items)
        if self.tool_outputs:
            return tuple(
                GatewayFunctionCallOutputInput(
                    tool_call_id=output.tool_call_id,
                    output=output.output,
                )
                for output in self.tool_outputs
            )
        if self.input is None:  # guarded by model validation
            raise ValueError("model input is required")
        return (
            GatewayUserMessageInput(
                message_id=self.request_id,
                content=self.input,
            ),
        )


class GatewayEvent(GatewayModel):
    schema_version: Literal[1] = 1
    seq: int = Field(ge=1, le=10_000)
    event_type: GatewayEventType
    response_id: str = Field(min_length=1, max_length=256)
    delta: str | None = Field(default=None, max_length=1_000_000)
    reasoning_id: str | None = Field(default=None, max_length=256)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=256)
    error_message: str | None = Field(default=None, max_length=2000)
    retryable: bool = False
    usage: dict[str, int] | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> "GatewayEvent":
        if self.delta is not None:
            _validate_json_value(self.delta, "text delta")
        if self.error_message is not None:
            _validate_json_value(self.error_message, "error message")
        for label, value in (
            ("response_id", self.response_id),
            ("reasoning_id", self.reasoning_id),
            ("tool_call_id", self.tool_call_id),
            ("tool_name", self.tool_name),
            ("idempotency_key", self.idempotency_key),
            ("error_code", self.error_code),
        ):
            _validate_id(value, label)
        if self.arguments is not None:
            _validate_json_value(self.arguments, "tool arguments")
        if self.usage is not None:
            if len(self.usage) > 32 or any(
                _SAFE_ID.fullmatch(key) is None
                or isinstance(value, bool)
                or not 0 <= value <= 1_000_000_000_000
                for key, value in self.usage.items()
            ):
                raise ValueError("gateway usage is invalid")
        if self.event_type is GatewayEventType.OUTPUT_TEXT_DELTA:
            if self.delta is None or any(
                value is not None
                for value in (
                    self.tool_call_id,
                    self.reasoning_id,
                    self.tool_name,
                    self.arguments,
                    self.idempotency_key,
                    self.error_code,
                    self.error_message,
                    self.usage,
                )
            ):
                raise ValueError("output_text.delta requires only delta content")
            if self.retryable:
                raise ValueError("output_text.delta cannot be retryable")
        elif self.event_type is GatewayEventType.REASONING_SUMMARY_DELTA:
            if (
                self.delta is None
                or not self.delta.strip()
                or not self.reasoning_id
                or any(
                    value is not None
                    for value in (
                        self.tool_call_id,
                        self.tool_name,
                        self.arguments,
                        self.idempotency_key,
                        self.error_code,
                        self.error_message,
                        self.usage,
                    )
                )
            ):
                raise ValueError(
                    "reasoning_summary.delta requires a reasoning identity and visible delta"
                )
            if self.retryable:
                raise ValueError("reasoning_summary.delta cannot be retryable")
        elif self.event_type is GatewayEventType.TOOL_CALL_REQUESTED:
            if not self.tool_call_id or not self.tool_name or self.arguments is None:
                raise ValueError("tool_call.requested is missing tool identity or arguments")
            if any(
                value is not None
                for value in (
                    self.delta,
                    self.reasoning_id,
                    self.error_code,
                    self.error_message,
                )
            ) or self.retryable:
                raise ValueError("tool_call.requested contains incompatible fields")
        elif self.event_type is GatewayEventType.RESPONSE_COMPLETED:
            if any(
                value is not None
                for value in (
                    self.delta,
                    self.reasoning_id,
                    self.tool_call_id,
                    self.tool_name,
                    self.arguments,
                    self.error_code,
                    self.error_message,
                    self.idempotency_key,
                )
            ):
                raise ValueError("response.completed contains incompatible fields")
            if self.retryable:
                raise ValueError("response.completed cannot be retryable")
        elif self.event_type is GatewayEventType.RESPONSE_FAILED:
            if not self.error_code or not self.error_message:
                raise ValueError("response.failed requires a stable error")
            if any(
                value is not None
                for value in (
                    self.delta,
                    self.reasoning_id,
                    self.tool_call_id,
                    self.tool_name,
                    self.arguments,
                    self.idempotency_key,
                    self.usage,
                )
            ):
                raise ValueError("response.failed contains incompatible fields")
        return self

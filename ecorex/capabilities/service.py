"""Progressive tool discovery and guarded invocation facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import hashlib
import inspect
import json
import threading
from typing import Any, Protocol
import uuid

from .errors import (
    ApprovalRequiredError,
    CapabilityDeniedError,
    CapabilityUnavailableError,
    IdempotencyKeyRequiredError,
    StaleCapabilitySnapshotError,
    ToolHandlerMissingError,
    ToolHandlerContractError,
    ToolArgumentsValidationError,
    ToolOutputValidationError,
    UnknownCapabilityError,
)
from .models import (
    CapabilityPlan,
    ExecutionPolicy,
    Exposure,
    RuntimeAvailability,
    SandboxLevel,
    ToolProviderProvenance,
    ToolSearchResult,
    ToolSpec,
    stable_digest,
)
from .planner import CapabilityPlanner, availability_reasons
from .policy import evaluate_governance
from .intent_routing import IntentRoutingPolicy, builtin_intent_routing_policy
from .discovery import DiscoveryPolicy, builtin_discovery_policy
from .discovery import (
    PROVIDER_REVIEWED_SLOT_DENOMINATOR,
    PROVIDER_REVIEWED_SLOT_NUMERATOR,
)
from .registry import CapabilityRegistry
from .repository import CapabilitySnapshotNotFound, CapabilitySnapshotRepository
from .schema import (
    SchemaInstanceError,
    canonical_json_value,
    validate_schema_instance,
)


ToolHandler = Callable[..., Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    invocation_id: str
    capability_snapshot_id: str
    policy_snapshot_id: str
    tool_id: str
    idempotency_key: str | None
    approved: bool
    effective_sandbox: SandboxLevel
    disclosure_granted: bool = False
    execution_scope: "ToolExecutionScope | None" = None
    tool_call_id: str | None = None
    current_policy_snapshot_id: str | None = None
    backend: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionScope:
    job_id: str
    thread_id: str
    turn_id: str
    execution_batch_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.job_id, self.thread_id, self.turn_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("tool execution scope contains an invalid identity")
        if self.execution_batch_id is not None and (
            not isinstance(self.execution_batch_id, str)
            or not self.execution_batch_id.strip()
            or len(self.execution_batch_id) > 256
        ):
            raise ValueError("tool execution scope contains an invalid batch identity")


class DeferredDisclosureAuthority(Protocol):
    """Backend authority for a snapshot-bound deferred-tool disclosure.

    The model-facing protocol may carry hints about what was disclosed, but
    only a Runtime-owned implementation of this protocol can authorize an
    invocation.  Keeping the protocol in the capability layer avoids a
    dependency on the Runtime's durable execution repository.
    """

    def verify(
        self,
        *,
        execution_scope: ToolExecutionScope,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        tool_version: str,
    ) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class ToolInvocationAdmission:
    """Runtime-issued, durable permit for one exact tool dispatch.

    The permit is not a caller-controlled approval flag.  It is reconstructed
    from the append-only Runtime admission record and binds the frozen Turn
    authority, the current permission fact observed at the linearization
    point, the canonical arguments, and the effective sandbox.
    """

    permit_id: str
    tool_call_id: str
    execution_scope: ToolExecutionScope
    capability_snapshot_id: str
    frozen_policy_snapshot_id: str
    current_policy_snapshot_id: str
    current_permission_state_digest: str
    current_availability_digest: str | None
    tool_id: str
    tool_version: str
    arguments_sha256: str
    idempotency_key: str | None
    approved: bool
    effective_sandbox: SandboxLevel
    admitted_at: str


class InvocationAdmissionAuthority(Protocol):
    """Runtime authority that resolves one exact persisted dispatch permit."""

    def resolve(
        self,
        *,
        execution_scope: ToolExecutionScope,
        tool_call_id: str,
        capability_snapshot_id: str,
        policy_snapshot_id: str,
        tool_id: str,
        tool_version: str,
        arguments_sha256: str,
        idempotency_key: str | None,
    ) -> ToolInvocationAdmission | None:
        ...


@dataclass(frozen=True, slots=True)
class ToolInvocationRecord:
    invocation_id: str
    capability_snapshot_id: str
    policy_snapshot_id: str
    tool_id: str
    tool_version: str
    provider: ToolProviderProvenance
    requested_reference: str
    arguments_sha256: str
    idempotency_key: str | None
    approved: bool
    disclosure_granted: bool
    effective_sandbox: str
    status: str
    created_at: str
    current_policy_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationGovernance:
    """The least-privileged intersection of frozen and current policy.

    A Turn's immutable snapshot remains the Replay fact, but a later revocation
    must take effect before a queued or resumed side effect.  A newer policy is
    therefore allowed to tighten an invocation and is never allowed to broaden
    the authority captured by the Turn.
    """

    allowed: bool
    requires_approval: bool
    effective_sandbox: SandboxLevel
    frozen_policy_snapshot_id: str
    current_policy_snapshot_id: str
    current_permission_state_digest: str | None
    current_admin_hard_denies: tuple[str, ...]
    current_availability_digest: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    value: Any
    record: ToolInvocationRecord


@dataclass(frozen=True, slots=True)
class _ToolSearchCandidate:
    result: ToolSearchResult
    match_rank: int
    match_specificity: int

    @property
    def order_key(self) -> tuple[Any, ...]:
        provider = self.result.provider
        return (
            -self.match_rank,
            0 if provider.product_reviewed else 1,
            -self.match_specificity,
            -self.result.score,
            -provider.trust_rank,
            provider.identity,
            self.result.tool_id,
        )


def _select_provider_fair_results(
    candidates: list[_ToolSearchCandidate],
    *,
    limit: int,
) -> tuple[ToolSearchResult, ...]:
    """Select a deterministic bounded result without provider monopolies.

    Exact references are resolved before any quota.  For broader discovery,
    a bounded reviewed reserve keeps Core/product contracts visible, then the
    least represented exact provider revision wins each next slot.  Grouping
    uses signed provenance and never parses authority from a tool ID.
    """

    ordered = sorted(candidates, key=lambda item: item.order_key)
    selected: list[_ToolSearchCandidate] = []
    selected_tool_ids: set[str] = set()
    provider_counts: dict[tuple[str, str, str], int] = {}

    def choose(candidate: _ToolSearchCandidate) -> None:
        selected.append(candidate)
        selected_tool_ids.add(candidate.result.tool_id)
        identity = candidate.result.provider.identity
        provider_counts[identity] = provider_counts.get(identity, 0) + 1

    # Exact references are unique in the sealed registry.  Keeping this as a
    # separate phase also makes limit=1 exact lookup independent of quotas.
    for candidate in ordered:
        if candidate.result.match_class != "exact_reference":
            continue
        choose(candidate)
        if len(selected) == limit:
            return tuple(item.result for item in selected)

    remaining_slots = limit - len(selected)
    reviewed_budget = (
        remaining_slots * PROVIDER_REVIEWED_SLOT_NUMERATOR
        + PROVIDER_REVIEWED_SLOT_DENOMINATOR
        - 1
    ) // PROVIDER_REVIEWED_SLOT_DENOMINATOR
    for candidate in ordered:
        if (
            reviewed_budget <= 0
            or candidate.result.tool_id in selected_tool_ids
            or not candidate.result.provider.product_reviewed
        ):
            continue
        choose(candidate)
        reviewed_budget -= 1
        if len(selected) == limit:
            return tuple(item.result for item in selected)

    remaining = [
        candidate
        for candidate in ordered
        if candidate.result.tool_id not in selected_tool_ids
    ]
    while remaining and len(selected) < limit:
        candidate = min(
            remaining,
            key=lambda item: (
                provider_counts.get(item.result.provider.identity, 0),
                *item.order_key,
            ),
        )
        choose(candidate)
        remaining.remove(candidate)
    return tuple(item.result for item in selected)


class CapabilityService:
    """In-process facade; Durable Job persists actual production executions."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        handlers: Mapping[str, ToolHandler] | None = None,
        audit_sink: Callable[[ToolInvocationRecord], None] | None = None,
        snapshot_repository: CapabilitySnapshotRepository | None = None,
        max_snapshots: int = 128,
        intent_routing_policy: IntentRoutingPolicy | None = None,
        discovery_policy: DiscoveryPolicy | None = None,
    ) -> None:
        if max_snapshots < 1:
            raise ValueError("max_snapshots must be positive")
        self.registry = registry
        # Runtime composition finishes Core/Pack/MCP registration before the
        # service exists.  From this point every snapshot and invocation must
        # observe one immutable catalog; later providers require a new Runtime
        # slot rather than mutating a live registry under queued Turns.
        self.registry.seal()
        routing_policy = intent_routing_policy or builtin_intent_routing_policy()
        self.discovery_policy = discovery_policy or builtin_discovery_policy(
            routing_policy
        )
        self.planner = CapabilityPlanner(
            registry,
            routing_policy=routing_policy,
            discovery_policy=self.discovery_policy,
        )
        self.handlers = dict(handlers or {})
        self.audit_sink = audit_sink
        self.snapshot_repository = snapshot_repository
        self.max_snapshots = max_snapshots
        self._snapshots: dict[str, CapabilityPlan] = {}
        self._snapshot_order: list[str] = []
        self._lock = threading.RLock()
        self._invocation_backend: Any | None = None
        self._disclosure_authority: DeferredDisclosureAuthority | None = None
        self._invocation_admission_authority: InvocationAdmissionAuthority | None = None
        self._current_policy_provider: Callable[[], ExecutionPolicy] | None = None
        self._current_permission_state_digest_provider: Callable[[], str] | None = None
        self._permission_sample_scope_provider: (
            Callable[[], AbstractContextManager[Any]] | None
        ) = None
        self._current_availability_provider: Callable[[], RuntimeAvailability] | None = None

    def bind_invocation_backend(self, backend: Any) -> None:
        """Bind the product backend exactly once after domain composition."""

        if backend is None:
            raise ValueError("tool invocation backend is required")
        with self._lock:
            if self._invocation_backend is not None and self._invocation_backend is not backend:
                raise RuntimeError("tool invocation backend is already bound")
            self._invocation_backend = backend

    def bind_disclosure_authority(
        self, authority: DeferredDisclosureAuthority
    ) -> None:
        """Bind the Runtime's durable disclosure verifier exactly once."""

        if authority is None or not callable(getattr(authority, "verify", None)):
            raise ValueError("deferred disclosure authority is invalid")
        with self._lock:
            if (
                self._disclosure_authority is not None
                and self._disclosure_authority is not authority
            ):
                raise RuntimeError("deferred disclosure authority is already bound")
            self._disclosure_authority = authority

    def bind_invocation_admission_authority(
        self, authority: InvocationAdmissionAuthority
    ) -> None:
        """Bind the Runtime's durable tool-dispatch permit resolver once."""

        if authority is None or not callable(getattr(authority, "resolve", None)):
            raise ValueError("tool invocation admission authority is invalid")
        with self._lock:
            if (
                self._invocation_admission_authority is not None
                and self._invocation_admission_authority is not authority
            ):
                raise RuntimeError(
                    "tool invocation admission authority is already bound"
                )
            self._invocation_admission_authority = authority

    def bind_current_policy_provider(
        self, provider: Callable[[], ExecutionPolicy]
    ) -> None:
        """Bind the Runtime-owned permission authority exactly once.

        The callback is evaluated at invocation admission, not at Turn
        creation.  It closes the window where revoking full access would leave
        an old queued/restarted Turn with danger-full-access and approval-never.
        """

        if not callable(provider):
            raise ValueError("current policy provider is invalid")
        with self._lock:
            if (
                self._current_policy_provider is not None
                and self._current_policy_provider is not provider
            ):
                raise RuntimeError("current policy provider is already bound")
            self._current_policy_provider = provider

    def bind_current_availability_provider(
        self, provider: Callable[[], RuntimeAvailability]
    ) -> None:
        """Bind just-in-time Pack/connector/network availability tightening."""

        if not callable(provider):
            raise ValueError("current capability availability provider is invalid")
        with self._lock:
            if (
                self._current_availability_provider is not None
                and self._current_availability_provider is not provider
            ):
                raise RuntimeError(
                    "current capability availability provider is already bound"
                )
            self._current_availability_provider = provider

    def bind_current_permission_state_digest_provider(
        self, provider: Callable[[], str]
    ) -> None:
        """Bind the verified permission-ledger chain head used by permits."""

        if not callable(provider):
            raise ValueError("current permission state digest provider is invalid")
        with self._lock:
            if (
                self._current_permission_state_digest_provider is not None
                and self._current_permission_state_digest_provider is not provider
            ):
                raise RuntimeError(
                    "current permission state digest provider is already bound"
                )
            self._current_permission_state_digest_provider = provider

    def bind_permission_sample_scope_provider(
        self,
        provider: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        """Bind one synchronous, operation-scoped permission sample context."""

        if not callable(provider):
            raise ValueError("permission sample scope provider is invalid")
        with self._lock:
            if (
                self._permission_sample_scope_provider is not None
                and self._permission_sample_scope_provider is not provider
            ):
                raise RuntimeError(
                    "permission sample scope provider is already bound"
                )
            self._permission_sample_scope_provider = provider

    def invocation_governance(
        self,
        capability_snapshot_id: str,
        reference: str,
    ) -> InvocationGovernance:
        """Resolve the non-broadening current-policy overlay for one tool."""

        plan = self._snapshot(capability_snapshot_id)
        spec = self.registry.resolve(reference)
        decision = plan.decision(spec.tool_id)
        if decision is None:
            raise UnknownCapabilityError(f"tool is absent from snapshot: {reference!r}")
        if spec.version != decision.tool_version:
            raise StaleCapabilitySnapshotError(
                "tool contract version changed after the capability snapshot"
            )
        with self._lock:
            provider = self._current_policy_provider
            availability_provider = self._current_availability_provider
            state_digest_provider = self._current_permission_state_digest_provider
            sample_scope_provider = self._permission_sample_scope_provider
        if provider is None:
            # Standalone CapabilityService users do not have a mutable Runtime
            # authority.  Preserve their frozen decision rather than guessing
            # the profile that produced it.
            return InvocationGovernance(
                allowed=decision.eligible,
                requires_approval=decision.requires_approval,
                effective_sandbox=decision.effective_sandbox,
                frozen_policy_snapshot_id=plan.policy_snapshot_id,
                current_policy_snapshot_id=plan.policy_snapshot_id,
                current_permission_state_digest=None,
                current_admin_hard_denies=(),
                current_availability_digest=None,
                reason_codes=("frozen_policy",),
            )
        sample_scope = (
            sample_scope_provider()
            if sample_scope_provider is not None
            else nullcontext()
        )
        with sample_scope:
            current = provider()
            if not isinstance(current, ExecutionPolicy):
                raise CapabilityDeniedError(
                    "current permission authority is unavailable"
                )
            current_decision = evaluate_governance(spec, current)
            current_permission_state_digest = (
                state_digest_provider()
                if state_digest_provider is not None
                else stable_digest({"permission_snapshot_id": current.snapshot_id})
            )
            if (
                not isinstance(current_permission_state_digest, str)
                or len(current_permission_state_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in current_permission_state_digest
                )
            ):
                raise CapabilityDeniedError(
                    "current permission ledger authority is unavailable"
                )
            current_availability_digest: str | None = None
            current_availability_reasons: tuple[str, ...] = ()
            if availability_provider is not None:
                current_availability = availability_provider()
                if not isinstance(current_availability, RuntimeAvailability):
                    raise CapabilityDeniedError(
                        "current capability availability authority is unavailable"
                    )
                # Model selection is immutable Turn context, not mutable
                # machine availability.  The live provider deliberately omits
                # a model choice because it is shared between concurrent
                # Turns; carrying that omission into this recheck would make
                # a tool such as imagegen appear to lose its model halfway
                # through the same Turn.  Keep the current machine/connectors
                # overlay, while restoring only the signed model capability
                # snapshot that created this capability plan.
                if plan.selected_model_capabilities is not None:
                    selected_model_capabilities = plan.selected_model_capabilities
                    current_availability = replace(
                        current_availability,
                        selected_model_modalities=frozenset(
                            selected_model_capabilities
                        ),
                        selected_model_capabilities=selected_model_capabilities,
                    )
                current_availability_reasons = availability_reasons(
                    spec, current_availability
                )
                current_availability_digest = stable_digest(
                    current_availability.to_dict()
                )
        sandbox_rank = {
            SandboxLevel.READ_ONLY: 0,
            SandboxLevel.WORKSPACE_WRITE: 1,
            SandboxLevel.DANGER_FULL_ACCESS: 2,
        }
        effective_sandbox = min(
            (decision.effective_sandbox, current_decision.effective_sandbox),
            key=sandbox_rank.__getitem__,
        )
        reasons = tuple(
            dict.fromkeys(
                (
                    "frozen_policy",
                    *(f"current:{code}" for code in current_decision.reason_codes),
                    *(
                        f"current_availability:{code}"
                        for code in current_availability_reasons
                    ),
                    *(
                        ("current_policy_tightened",)
                        if current.snapshot_id != plan.policy_snapshot_id
                        else ()
                    ),
                )
            )
        )
        return InvocationGovernance(
            allowed=(
                decision.eligible
                and current_decision.allowed
                and not current_availability_reasons
            ),
            requires_approval=(
                decision.requires_approval or current_decision.requires_approval
            ),
            effective_sandbox=effective_sandbox,
            frozen_policy_snapshot_id=plan.policy_snapshot_id,
            current_policy_snapshot_id=current.snapshot_id,
            current_permission_state_digest=current_permission_state_digest,
            current_admin_hard_denies=tuple(sorted(current.admin_hard_denies)),
            current_availability_digest=current_availability_digest,
            reason_codes=reasons,
        )

    def create_plan(
        self,
        *,
        intent: str,
        explicit_tools: tuple[str, ...] = (),
        runtime_direct_tools: tuple[str, ...] = (),
        availability: RuntimeAvailability,
        policy: ExecutionPolicy,
    ) -> CapabilityPlan:
        plan = self.planner.plan(
            intent=intent,
            explicit_tools=explicit_tools,
            runtime_direct_tools=runtime_direct_tools,
            availability=availability,
            policy=policy,
        )
        if self.snapshot_repository is not None:
            self.snapshot_repository.save(plan)
        with self._lock:
            if plan.snapshot_id not in self._snapshots:
                self._snapshot_order.append(plan.snapshot_id)
            self._snapshots[plan.snapshot_id] = plan
            while len(self._snapshot_order) > self.max_snapshots:
                expired = self._snapshot_order.pop(0)
                self._snapshots.pop(expired, None)
        return plan

    def tool_search(
        self,
        capability_snapshot_id: str,
        query: str,
        *,
        limit: int = 10,
        exposure: Exposure | None = None,
        model_catalog_payload: Mapping[str, Any] | None = None,
    ) -> tuple[ToolSearchResult, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("tool search limit must be between 1 and 50")
        if exposure is not None and not isinstance(exposure, Exposure):
            raise ValueError("tool search exposure scope is invalid")
        plan = self._snapshot(capability_snapshot_id)
        if plan.discovery_policy_digest != self.discovery_policy.digest:
            raise StaleCapabilitySnapshotError(
                "discovery policy changed after the capability snapshot"
            )
        candidates: list[_ToolSearchCandidate] = []
        for decision in plan.decisions:
            if not decision.eligible or decision.exposure is Exposure.HIDDEN:
                continue
            # Scope must be applied before ranking and truncation.  Runtime's
            # meta-tool searches only deferred capabilities; otherwise a
            # matching direct tool could consume ``limit=1`` and produce an
            # empty result after a later filter.
            if exposure is not None and decision.exposure is not exposure:
                continue
            spec = self.registry.get(decision.tool_id)
            match = self.discovery_policy.match(
                query,
                spec,
                model_catalog_payload=model_catalog_payload,
            )
            if match is None:
                continue
            # Match class dominates. Planner score is a bounded final
            # tie-break and cannot let keyword stuffing outrank an exact
            # reference, reviewed facet or frozen model alias.
            search_score = (
                match.rank * 1_000_000
                + min(match.specificity, 9_999) * 10_000
                + max(-9_999, min(decision.score, 9_999))
            )
            result = ToolSearchResult(
                    discovery_id=f"tool:{spec.tool_id}@{spec.version}",
                    tool_id=spec.tool_id,
                    tool_version=spec.version,
                    display_name=spec.display_name,
                    description=spec.description,
                    exposure=decision.exposure,
                    score=search_score,
                    requires_approval=decision.requires_approval,
                    match_class=match.match_class,
                    matched_facets=match.matched_facets,
                    matched_evidence=match.evidence,
                    provider=spec.provider,
                )
            candidates.append(
                _ToolSearchCandidate(
                    result=result,
                    match_rank=match.rank,
                    match_specificity=match.specificity,
                )
            )
        return _select_provider_fair_results(candidates, limit=limit)

    def get_plan(self, capability_snapshot_id: str) -> CapabilityPlan:
        """Return the immutable replay/audit projection for a Turn snapshot."""

        return self._snapshot(capability_snapshot_id)

    def tool_describe(
        self,
        capability_snapshot_id: str,
        reference: str,
    ) -> dict[str, Any]:
        plan = self._snapshot(capability_snapshot_id)
        requested_version: str | None = None
        canonical_reference = reference
        if reference.startswith("tool:"):
            canonical_reference, separator, requested_version = reference[5:].rpartition("@")
            if not separator or not canonical_reference or not requested_version:
                raise UnknownCapabilityError(
                    f"invalid discovery identity: {reference!r}"
                )
        spec = self.registry.resolve(canonical_reference)
        decision = plan.decision(spec.tool_id)
        if decision is None:
            raise UnknownCapabilityError(f"tool is absent from snapshot: {reference!r}")
        if spec.version != decision.tool_version:
            raise StaleCapabilitySnapshotError(
                "tool contract version changed after the capability snapshot"
            )
        if requested_version is not None and requested_version != decision.tool_version:
            raise UnknownCapabilityError(
                f"tool discovery version is absent from snapshot: {reference!r}"
            )
        return {"spec": spec.to_dict(), "decision": decision.to_dict()}

    def validate_tool_arguments(
        self,
        capability_snapshot_id: str,
        reference: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate/canonicalize arguments without dispatch or side effects."""

        plan = self._snapshot(capability_snapshot_id)
        spec = self.registry.resolve(reference)
        decision = plan.decision(spec.tool_id)
        if decision is None or spec.version != decision.tool_version:
            raise StaleCapabilitySnapshotError(
                "tool contract is absent or changed before argument validation"
            )
        try:
            canonical_arguments = canonical_json_value(
                arguments,
                label=f"arguments for {spec.tool_id!r}",
            )
            validate_schema_instance(
                canonical_arguments,
                spec.input_schema,
                label=f"arguments for {spec.tool_id!r}",
            )
        except SchemaInstanceError as exc:
            raise ToolArgumentsValidationError(str(exc)) from None
        if not isinstance(canonical_arguments, dict):
            raise ToolArgumentsValidationError(
                f"arguments for {spec.tool_id!r} must be an object"
            )
        return canonical_arguments

    async def tool_call(
        self,
        capability_snapshot_id: str,
        reference: str,
        arguments: Mapping[str, Any],
        *,
        policy_snapshot_id: str,
        approved: bool = False,
        idempotency_key: str | None = None,
        execution_scope: ToolExecutionScope | None = None,
        disclosure_granted: bool = False,
        tool_call_id: str | None = None,
    ) -> ToolCallResult:
        plan = self._snapshot(capability_snapshot_id)
        if plan.policy_snapshot_id != policy_snapshot_id:
            raise StaleCapabilitySnapshotError("policy snapshot changed before invocation")
        spec = self.registry.resolve(reference)
        decision = plan.decision(spec.tool_id)
        if decision is None:
            raise UnknownCapabilityError(f"tool is absent from snapshot: {reference!r}")
        if spec.version != decision.tool_version:
            raise StaleCapabilitySnapshotError(
                "tool contract version changed after the capability snapshot"
            )
        if not decision.eligible:
            if any(code.startswith(("missing_", "disabled:", "platform_", "offline")) for code in decision.reason_codes):
                raise CapabilityUnavailableError(
                    f"tool {spec.tool_id!r} is unavailable: {', '.join(decision.reason_codes)}"
                )
            raise CapabilityDeniedError(
                f"tool {spec.tool_id!r} is denied: {', '.join(decision.reason_codes)}"
            )
        if decision.exposure is Exposure.HIDDEN:
            raise CapabilityDeniedError(f"tool {spec.tool_id!r} is hidden")
        disclosure_authorized = decision.exposure is Exposure.DIRECT
        if decision.exposure is Exposure.DEFERRED:
            # ``disclosure_granted`` is retained as an input compatibility hint
            # while callers migrate, but it deliberately has no authority.  A
            # completed, matching tool_describe fact in the durable Runtime is
            # the only way to cross the deferred invocation boundary.
            with self._lock:
                disclosure_authority = self._disclosure_authority
            if (
                isinstance(execution_scope, ToolExecutionScope)
                and disclosure_authority is not None
            ):
                disclosure_authorized = (
                    disclosure_authority.verify(
                        execution_scope=execution_scope,
                        capability_snapshot_id=plan.snapshot_id,
                        policy_snapshot_id=plan.policy_snapshot_id,
                        tool_id=spec.tool_id,
                        tool_version=spec.version,
                    )
                    is True
                )
            if not disclosure_authorized:
                raise CapabilityDeniedError(
                    f"tool {spec.tool_id!r} has not been disclosed for this execution"
                )
        handler = self.handlers.get(spec.tool_id)
        if handler is None:
            raise ToolHandlerMissingError(f"no handler is registered for {spec.tool_id!r}")
        try:
            canonical_arguments = canonical_json_value(
                arguments,
                label=f"arguments for {spec.tool_id!r}",
            )
            validate_schema_instance(
                canonical_arguments,
                spec.input_schema,
                label=f"arguments for {spec.tool_id!r}",
            )
            encoded = json.dumps(
                canonical_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except SchemaInstanceError as exc:
            raise ToolArgumentsValidationError(str(exc)) from None
        arguments_sha256 = hashlib.sha256(encoded).hexdigest()

        admission: ToolInvocationAdmission | None = None
        if tool_call_id is not None:
            if (
                not isinstance(tool_call_id, str)
                or not tool_call_id.strip()
                or len(tool_call_id) > 256
                or not isinstance(execution_scope, ToolExecutionScope)
            ):
                raise CapabilityDeniedError("tool invocation admission scope is invalid")
            with self._lock:
                admission_authority = self._invocation_admission_authority
            if admission_authority is None:
                raise CapabilityDeniedError(
                    "durable tool invocation admission authority is unavailable"
                )
            admission = admission_authority.resolve(
                execution_scope=execution_scope,
                tool_call_id=tool_call_id,
                capability_snapshot_id=plan.snapshot_id,
                policy_snapshot_id=plan.policy_snapshot_id,
                tool_id=spec.tool_id,
                tool_version=spec.version,
                arguments_sha256=arguments_sha256,
                idempotency_key=idempotency_key,
            )
            if admission is None:
                raise CapabilityDeniedError(
                    f"tool {spec.tool_id!r} has no durable invocation admission"
                )

        if admission is None:
            invocation_governance = self.invocation_governance(
                plan.snapshot_id,
                spec.tool_id,
            )
            if not invocation_governance.allowed:
                raise CapabilityDeniedError(
                    f"tool {spec.tool_id!r} is denied by the current permission policy"
                )
            if invocation_governance.requires_approval and not approved:
                raise ApprovalRequiredError(f"tool {spec.tool_id!r} requires approval")
            effective_approved = approved
            effective_sandbox = invocation_governance.effective_sandbox
            current_policy_snapshot_id = (
                invocation_governance.current_policy_snapshot_id
            )
        else:
            # Once the durable permit has linearized against permission
            # mutation, it is the only authority consumed by dispatch.  A bare
            # caller boolean can neither create nor broaden this fact.
            effective_approved = admission.approved
            effective_sandbox = admission.effective_sandbox
            current_policy_snapshot_id = admission.current_policy_snapshot_id
        if spec.requires_idempotency_key and not str(idempotency_key or "").strip():
            raise IdempotencyKeyRequiredError(
                f"tool {spec.tool_id!r} requires an idempotency key"
            )
        invocation_id = "invoke_" + uuid.uuid4().hex
        context = ToolInvocationContext(
            invocation_id=invocation_id,
            capability_snapshot_id=plan.snapshot_id,
            policy_snapshot_id=plan.policy_snapshot_id,
            tool_id=spec.tool_id,
            idempotency_key=idempotency_key,
            approved=effective_approved,
            disclosure_granted=disclosure_authorized,
            effective_sandbox=effective_sandbox,
            execution_scope=execution_scope,
            tool_call_id=tool_call_id,
            current_policy_snapshot_id=current_policy_snapshot_id,
            backend=self._invocation_backend,
        )
        try:
            signature = inspect.signature(handler)
            parameters = tuple(signature.parameters.values())
            accepts_context = any(
                parameter.kind is inspect.Parameter.VAR_POSITIONAL
                for parameter in parameters
            ) or len(
                [
                    parameter
                    for parameter in parameters
                    if parameter.kind
                    in {
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    }
                ]
            ) >= 2
        except (TypeError, ValueError):
            accepts_context = False
        if spec.requires_idempotency_key and not accepts_context:
            raise ToolHandlerContractError(
                f"write/network handler for {spec.tool_id!r} must accept ToolInvocationContext"
            )
        value = (
            handler(canonical_arguments, context)
            if accepts_context
            else handler(canonical_arguments)
        )
        if inspect.isawaitable(value):
            value = await value
        try:
            value = canonical_json_value(
                value,
                label=f"output from {spec.tool_id!r}",
            )
            validate_schema_instance(
                value,
                spec.output_schema,
                label=f"output from {spec.tool_id!r}",
            )
        except SchemaInstanceError as exc:
            raise ToolOutputValidationError(str(exc)) from None
        record = ToolInvocationRecord(
            invocation_id=invocation_id,
            capability_snapshot_id=plan.snapshot_id,
            policy_snapshot_id=plan.policy_snapshot_id,
            tool_id=spec.tool_id,
            tool_version=spec.version,
            provider=spec.provider,
            requested_reference=reference,
            arguments_sha256=arguments_sha256,
            idempotency_key=idempotency_key,
            approved=effective_approved,
            disclosure_granted=context.disclosure_granted,
            effective_sandbox=effective_sandbox.value,
            status="completed",
            created_at=datetime.now(UTC).isoformat(),
            current_policy_snapshot_id=current_policy_snapshot_id,
        )
        if self.audit_sink is not None:
            self.audit_sink(record)
        return ToolCallResult(value=value, record=record)

    def _snapshot(self, snapshot_id: str) -> CapabilityPlan:
        with self._lock:
            plan = self._snapshots.get(snapshot_id)
        if plan is None and self.snapshot_repository is not None:
            try:
                plan = self.snapshot_repository.get(snapshot_id)
            except CapabilitySnapshotNotFound:
                plan = None
            else:
                with self._lock:
                    self._snapshots[snapshot_id] = plan
                    self._snapshot_order.append(snapshot_id)
                    while len(self._snapshot_order) > self.max_snapshots:
                        expired = self._snapshot_order.pop(0)
                        self._snapshots.pop(expired, None)
        if plan is None:
            raise StaleCapabilitySnapshotError(
                f"unknown or expired capability snapshot: {snapshot_id!r}"
            )
        if plan.catalog_digest != self.registry.digest:
            raise StaleCapabilitySnapshotError(
                "capability catalog changed after the capability snapshot"
            )
        return plan

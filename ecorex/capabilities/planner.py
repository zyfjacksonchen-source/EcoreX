"""Catalog -> availability -> governance -> exposure planner."""

from __future__ import annotations

from .models import (
    CapabilityDecision,
    CapabilityEffect,
    CapabilityPlan,
    ExecutionPolicy,
    Exposure,
    RuntimeAvailability,
    ToolSpec,
    normalize_reference,
    stable_digest,
)
from .intent_routing import (
    MAX_ROUTING_INTENT_BYTES,
    MAX_ROUTING_SCORE_BOOST,
    IntentRoutingPolicy,
    builtin_intent_routing_policy,
)
from .discovery import DiscoveryPolicy, builtin_discovery_policy
from .errors import CapabilityIntentError, UnknownCapabilityError
from .policy import evaluate_governance
from .registry import CapabilityRegistry


_MAX_GENERIC_MATCH_TOKENS = 32
_MAX_GENERIC_EVIDENCE = 16
_MAX_DECISION_TRACE_ITEMS = 128
_MAX_EXPLICIT_TOOLS = 64
_MAX_EXPLICIT_REFERENCE_BYTES = 512
_EXPLICIT_REFERENCE_SCORE = MAX_ROUTING_SCORE_BOOST + 1_000
_INTENT_REFERENCE_SCORE = 500


def _intent_exact_reference(intent: str, spec: ToolSpec) -> str | None:
    """Return an exact Core tool/alias mention that expresses usable intent.

    This is intentionally bounded and conservative. It is not fuzzy intent
    classification: it only recognizes catalog-owned names and rejects local
    negation/failure-discussion contexts. The result affects exposure, never
    availability or governance.
    """

    normalized = normalize_reference(intent)
    blockers = (
        "不要", "别", "無需", "无需", "不能", "无法",
        "do-not", "don-t", "dont", "without", "failed", "broken",
    )
    for reference in spec.references:
        if len(reference) < 3:
            continue
        start = normalized.find(reference)
        while start >= 0:
            before = normalized[max(0, start - 24):start]
            after = normalized[start + len(reference):start + len(reference) + 24]
            if not any(token in before or token in after for token in blockers):
                return reference
            start = normalized.find(reference, start + len(reference))
    return None


def _bounded_unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))[:_MAX_DECISION_TRACE_ITEMS]


def availability_reasons(
    spec: ToolSpec,
    availability: RuntimeAvailability,
) -> tuple[str, ...]:
    reasons: list[str] = []
    disabled = {
        normalize_reference(reference): reason
        for reference, reason in availability.disabled_tools.items()
    }
    for reference in spec.references:
        if reference in disabled:
            reasons.append(f"disabled:{disabled[reference]}")
            break
    platform = normalize_reference(availability.platform)
    supported = {normalize_reference(value) for value in spec.supported_platforms}
    if supported and platform not in supported:
        reasons.append("platform_unavailable")
    missing_packs = sorted(spec.required_packs - availability.installed_packs)
    if missing_packs:
        reasons.append("missing_packs:" + ",".join(missing_packs))
    missing_connectors = sorted(
        spec.required_connectors - availability.connected_connectors
    )
    if missing_connectors:
        reasons.append("missing_connectors:" + ",".join(missing_connectors))
    if availability.selected_model_modalities is not None:
        missing_modalities = sorted(
            spec.required_model_modalities
            - availability.selected_model_modalities
        )
        if missing_modalities:
            reasons.append("missing_model_modalities:" + ",".join(missing_modalities))
        if (
            spec.required_model_capabilities
            and availability.selected_model_capabilities is None
        ):
            reasons.append("missing_model_capabilities_snapshot")
        elif availability.selected_model_capabilities is not None:
            for modality, required in sorted(
                spec.required_model_capabilities.items()
            ):
                # A missing selected modality already has one precise reason;
                # do not duplicate it as every feature being absent.
                if modality in missing_modalities:
                    continue
                selected = availability.selected_model_capabilities.get(
                    modality, frozenset()
                )
                missing_capabilities = sorted(required - selected)
                if missing_capabilities:
                    reasons.append(
                        "missing_model_capabilities:"
                        + modality
                        + ":"
                        + ",".join(missing_capabilities)
                    )
    if CapabilityEffect.NETWORK in spec.effects and not availability.online:
        reasons.append("offline")
    return tuple(reasons)


class CapabilityPlanner:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        routing_policy: IntentRoutingPolicy | None = None,
        discovery_policy: DiscoveryPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.routing_policy = routing_policy or builtin_intent_routing_policy()
        self.discovery_policy = discovery_policy or builtin_discovery_policy(
            self.routing_policy
        )
        if self.discovery_policy.routing_policy_digest != self.routing_policy.digest:
            raise ValueError("discovery policy does not match the routing policy")

    def plan(
        self,
        *,
        intent: str,
        explicit_tools: tuple[str, ...] = (),
        runtime_direct_tools: tuple[str, ...] = (),
        availability: RuntimeAvailability,
        policy: ExecutionPolicy,
    ) -> CapabilityPlan:
        if not isinstance(intent, str):
            raise CapabilityIntentError("capability intent must be a string")
        try:
            intent_size = len(intent.encode("utf-8"))
        except UnicodeEncodeError:
            raise CapabilityIntentError(
                "capability intent must be valid Unicode"
            ) from None
        if (
            not isinstance(explicit_tools, tuple)
            or len(explicit_tools) > _MAX_EXPLICIT_TOOLS
            or any(not isinstance(reference, str) for reference in explicit_tools)
        ):
            raise ValueError("explicit tool references are invalid")
        if (
            not isinstance(runtime_direct_tools, tuple)
            or len(runtime_direct_tools) > _MAX_EXPLICIT_TOOLS
            or any(not isinstance(reference, str) for reference in runtime_direct_tools)
        ):
            raise ValueError("runtime direct tool references are invalid")
        for reference in explicit_tools:
            try:
                reference_size = len(reference.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("explicit tool reference must be valid Unicode") from None
            if reference_size > _MAX_EXPLICIT_REFERENCE_BYTES:
                raise ValueError("explicit tool reference exceeds the product limit")
        runtime_direct_ids: set[str] = set()
        for reference in runtime_direct_tools:
            try:
                reference_size = len(reference.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("runtime direct tool reference must be valid Unicode") from None
            if reference_size > _MAX_EXPLICIT_REFERENCE_BYTES:
                raise ValueError("runtime direct tool reference exceeds the product limit")
            try:
                runtime_direct_ids.add(self.registry.resolve(reference).tool_id)
            except UnknownCapabilityError:
                raise ValueError(
                    f"runtime direct tool selection is unavailable: {reference!r}"
                ) from None

        # The immutable snapshot still records a long, valid user intent, but
        # it cannot force unbounded normalization/search work in the routing
        # path.  Protocol admission owns the user-message size limit.
        normalized_intent = (
            normalize_reference(intent)
            if intent_size <= MAX_ROUTING_INTENT_BYTES
            else ""
        )
        explicit_ids: dict[str, list[str]] = {}
        unresolved: list[str] = []
        for reference in explicit_tools:
            try:
                resolved = self.registry.resolve(reference).tool_id
                explicit_ids.setdefault(resolved, []).append(
                    normalize_reference(reference)
                )
            except UnknownCapabilityError:
                unresolved.append(str(reference))

        intent_tokens = tuple(
            dict.fromkeys(
                token
                for token in normalized_intent.split("-")
                if 1 < len(token) <= 64
            )
        )[:_MAX_GENERIC_MATCH_TOKENS]
        decisions: list[CapabilityDecision] = []
        for spec in self.registry.all():
            current_availability_reasons = availability_reasons(spec, availability)
            governance = evaluate_governance(spec, policy)
            eligible = not current_availability_reasons and governance.allowed
            reasons = [*current_availability_reasons, *governance.reason_codes]
            matched_evidence: list[str] = []
            suppression_reasons = [
                f"availability:{reason}" for reason in current_availability_reasons
            ]
            if not governance.allowed:
                suppression_reasons.extend(
                    f"governance:{reason}" for reason in governance.reason_codes
                )

            score = (
                50 if spec.default_exposure is Exposure.DIRECT else 20
            ) + spec.priority_bias
            exposure = spec.default_exposure
            if spec.default_exposure is Exposure.HIDDEN:
                eligible = False
                reasons.append("catalog_hidden")
                suppression_reasons.append("catalog:catalog_hidden")

            searchable = " ".join(
                [
                    normalize_reference(spec.tool_id),
                    normalize_reference(spec.display_name),
                    normalize_reference(spec.description),
                    *(normalize_reference(alias) for alias in spec.aliases),
                    *(normalize_reference(tag) for tag in spec.intent_tags),
                ]
            )
            for token in intent_tokens:
                if token in searchable:
                    score += 5
                    if len(matched_evidence) < _MAX_GENERIC_EVIDENCE:
                        matched_evidence.append(f"catalog_token:{token}")

            route_evidence = self.routing_policy.evaluate(intent, spec)
            accepted_route_boosts: list[int] = []
            for evidence in route_evidence:
                identity = f"{evidence.rule_id}@{evidence.rule_version}"
                for term in evidence.matched_terms:
                    matched_evidence.append(f"intent_route:{identity}:{term}")
                if evidence.suppressed_by:
                    reasons.append(f"intent_route_suppressed:{identity}")
                    suppression_reasons.extend(
                        f"intent_route:{identity}:{term}"
                        for term in evidence.suppressed_by
                    )
                    continue
                if evidence.matched:
                    accepted_route_boosts.append(evidence.score_delta)
                    reasons.append(f"intent_route_matched:{identity}")
                    if evidence.promote_to is Exposure.DIRECT:
                        exposure = Exposure.DIRECT
            # Multiple rules can explain the same desired output (for example
            # create + edit).  Use the strongest reviewed route, rather than
            # stacking boosts by repeating vocabulary or metadata facets.
            if accepted_route_boosts:
                score += max(accepted_route_boosts)
            intent_reference = _intent_exact_reference(intent, spec)
            route_reference_suppressed = (
                bool(route_evidence)
                and not accepted_route_boosts
                and any(evidence.suppressed_by for evidence in route_evidence)
            )
            if (
                intent_reference is not None
                and spec.tool_id not in explicit_ids
                and exposure is not Exposure.HIDDEN
                and not route_reference_suppressed
            ):
                score += _INTENT_REFERENCE_SCORE
                exposure = Exposure.DIRECT
                reasons.append("intent_exact_reference")
                matched_evidence.insert(
                    0, f"intent_exact_reference:{intent_reference}"
                )
            if spec.tool_id in explicit_ids:
                # Explicit eligible tool choice is the strongest routing fact.
                # The constant is defined above the maximum possible policy
                # boost so a semantic hint can never override the user's exact
                # tool selection.
                score += _EXPLICIT_REFERENCE_SCORE
                if exposure is not Exposure.HIDDEN and not route_reference_suppressed:
                    exposure = Exposure.DIRECT
                reasons.append("explicit_reference")
                if route_reference_suppressed:
                    reasons.append("explicit_reference_suppressed_by_intent")
                    suppression_reasons.append(
                        "explicit_reference:intent_route_suppressed"
                    )
                # Preserve exact user selection in the bounded trace even if
                # a custom reviewed policy supplies the maximum rule count.
                matched_evidence[0:0] = [
                    f"explicit_reference:{reference}"
                    for reference in explicit_ids[spec.tool_id]
                ]
            if spec.tool_id in runtime_direct_ids:
                # Core may expose a small capability only because immutable
                # Turn context needs it (for example a bound input
                # attachment). This is intentionally separate from an
                # explicit user selection and never bypasses governance.
                score += _EXPLICIT_REFERENCE_SCORE
                if exposure is not Exposure.HIDDEN:
                    exposure = Exposure.DIRECT
                reasons.append("runtime_context_required")
                matched_evidence.insert(0, f"runtime_context:{spec.tool_id}")
            if not eligible:
                exposure = Exposure.HIDDEN

            decisions.append(
                CapabilityDecision(
                    tool_id=spec.tool_id,
                    tool_version=spec.version,
                    exposure=exposure,
                    eligible=eligible,
                    # Availability and governance are independent facts.  A
                    # missing pack keeps the tool ineligible, but must not
                    # rewrite the current approval policy in the immutable
                    # decision snapshot used by diagnostics and Live Replay.
                    requires_approval=governance.requires_approval,
                    effective_sandbox=governance.effective_sandbox,
                    score=score,
                    reason_codes=_bounded_unique(reasons),
                    matched_evidence=_bounded_unique(matched_evidence),
                    suppression_reasons=_bounded_unique(suppression_reasons),
                    provider=spec.provider,
                )
            )

        decisions.sort(
            key=lambda item: (
                0 if item.eligible else 1,
                -item.score,
                item.tool_id,
            )
        )
        snapshot_payload = {
            "catalog_digest": self.registry.digest,
            "routing_policy": {
                "policy_id": self.routing_policy.policy_id,
                "version": self.routing_policy.version,
                "digest": self.routing_policy.digest,
            },
            "discovery_policy": {
                "policy_id": self.discovery_policy.policy_id,
                "version": self.discovery_policy.version,
                "digest": self.discovery_policy.digest,
            },
            "availability": availability.to_dict(),
            "policy": policy.to_dict(),
            "intent": intent,
            "explicit_tools": list(explicit_tools),
            "runtime_direct_tools": list(runtime_direct_tools),
            "decisions": [decision.to_dict() for decision in decisions],
            "unresolved_explicit": unresolved,
        }
        return CapabilityPlan(
            snapshot_id="cap_" + stable_digest(snapshot_payload),
            policy_snapshot_id=policy.snapshot_id,
            intent=intent,
            decisions=tuple(decisions),
            catalog_digest=self.registry.digest,
            unresolved_explicit=tuple(unresolved),
            runtime_direct_tools=tuple(
                sorted(runtime_direct_ids)
            ),
            routing_policy_id=self.routing_policy.policy_id,
            routing_policy_version=self.routing_policy.version,
            routing_policy_digest=self.routing_policy.digest,
            discovery_policy_id=self.discovery_policy.policy_id,
            discovery_policy_version=self.discovery_policy.version,
            discovery_policy_digest=self.discovery_policy.digest,
            selected_model_capabilities=availability.selected_model_capabilities,
        )

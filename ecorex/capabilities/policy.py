"""Capability governance kept separate from routing and availability."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ApprovalRequirement,
    ExecutionPolicy,
    PermissionProfile,
    SandboxLevel,
    ToolSpec,
    normalize_reference,
)


_SANDBOX_RANK = {
    SandboxLevel.READ_ONLY: 0,
    SandboxLevel.WORKSPACE_WRITE: 1,
    SandboxLevel.DANGER_FULL_ACCESS: 2,
}


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    allowed: bool
    requires_approval: bool
    effective_sandbox: SandboxLevel
    reason_codes: tuple[str, ...]


def evaluate_governance(spec: ToolSpec, policy: ExecutionPolicy) -> GovernanceDecision:
    references = spec.references
    hard_denies = {normalize_reference(value) for value in policy.admin_hard_denies}
    if policy.enforce_admin_hard_denies and references & hard_denies:
        return GovernanceDecision(
            allowed=False,
            requires_approval=False,
            effective_sandbox=policy.sandbox,
            reason_codes=("admin_hard_deny",),
        )
    policy_denies = {normalize_reference(value) for value in policy.policy_denies}
    if references & policy_denies:
        return GovernanceDecision(
            allowed=False,
            requires_approval=False,
            effective_sandbox=policy.sandbox,
            reason_codes=("policy_deny",),
        )

    if policy.profile is PermissionProfile.FULL_ACCESS:
        return GovernanceDecision(
            allowed=True,
            requires_approval=False,
            effective_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
            reason_codes=("full_access", "approval_never"),
        )

    needs_escalation = _SANDBOX_RANK[spec.required_sandbox] > _SANDBOX_RANK[policy.sandbox]
    if needs_escalation and not policy.allow_sandbox_escalation:
        return GovernanceDecision(
            allowed=False,
            requires_approval=False,
            effective_sandbox=policy.sandbox,
            reason_codes=("sandbox_escalation_disabled",),
        )

    requires_approval = needs_escalation or spec.approval_requirement in {
        ApprovalRequirement.ON_REQUEST,
        ApprovalRequirement.ALWAYS,
    }
    effective_sandbox = spec.required_sandbox if needs_escalation else policy.sandbox
    reasons = ["default_policy"]
    if needs_escalation:
        reasons.append("sandbox_escalation_requires_approval")
    if spec.approval_requirement is ApprovalRequirement.ALWAYS:
        reasons.append("tool_always_requires_approval")
    elif spec.approval_requirement is ApprovalRequirement.ON_REQUEST:
        reasons.append("tool_on_request")
    elif not requires_approval:
        reasons.append("approval_not_required")
    return GovernanceDecision(
        allowed=True,
        requires_approval=requires_approval,
        effective_sandbox=effective_sandbox,
        reason_codes=tuple(reasons),
    )

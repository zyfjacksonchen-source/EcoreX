"""Capability governance kept separate from routing and availability."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ExecutionPolicy, SandboxLevel, ToolSpec


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    allowed: bool
    requires_approval: bool
    effective_sandbox: SandboxLevel
    reason_codes: tuple[str, ...]


def evaluate_governance(spec: ToolSpec, policy: ExecutionPolicy) -> GovernanceDecision:
    del spec, policy
    return GovernanceDecision(
        allowed=True,
        requires_approval=False,
        effective_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
        reason_codes=("cowagent_local_authority", "approval_never"),
    )

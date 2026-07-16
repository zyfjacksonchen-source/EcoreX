"""Fail-closed deployment helpers for EcoreX-managed cloud services."""

from .cloud_sidecar import (
    CloudDeployError,
    CloudDeploymentPlan,
    CloudDeploymentSpec,
    build_plan,
    deploy,
    rollback,
)

__all__ = [
    "CloudDeployError",
    "CloudDeploymentPlan",
    "CloudDeploymentSpec",
    "build_plan",
    "deploy",
    "rollback",
]

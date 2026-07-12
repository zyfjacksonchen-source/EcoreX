"""Stable errors for the safe office-output domain."""

from __future__ import annotations


class OutputError(RuntimeError):
    """Base error safe for an API adapter to map without exposing host paths."""

    code = "OUTPUT_ERROR"


class OutputValidationError(OutputError):
    code = "OUTPUT_VALIDATION_ERROR"


class OutputLocationUnavailable(OutputError):
    code = "OUTPUT_LOCATION_UNAVAILABLE"


class OutputRevisionConflict(OutputError):
    code = "OUTPUT_REVISION_CONFLICT"


class OutputIdempotencyConflict(OutputError):
    code = "OUTPUT_IDEMPOTENCY_CONFLICT"


class OutputPolicyNotFound(OutputError):
    code = "OUTPUT_POLICY_NOT_FOUND"


class OutputPolicyBindingMissing(OutputError):
    code = "OUTPUT_POLICY_BINDING_MISSING"


class OutputArtifactNotEligible(OutputError):
    code = "OUTPUT_ARTIFACT_NOT_ELIGIBLE"


class OutputRootUnsafe(OutputError):
    code = "OUTPUT_ROOT_UNSAFE"


class OutputRootChanged(OutputError):
    code = "OUTPUT_ROOT_CHANGED"


class OutputIntegrityError(OutputError):
    code = "OUTPUT_INTEGRITY_ERROR"


class OutputMaterializationFailed(OutputError):
    code = "OUTPUT_MATERIALIZATION_FAILED"


__all__ = [
    "OutputArtifactNotEligible",
    "OutputError",
    "OutputIdempotencyConflict",
    "OutputIntegrityError",
    "OutputLocationUnavailable",
    "OutputMaterializationFailed",
    "OutputPolicyBindingMissing",
    "OutputPolicyNotFound",
    "OutputRevisionConflict",
    "OutputRootChanged",
    "OutputRootUnsafe",
    "OutputValidationError",
]

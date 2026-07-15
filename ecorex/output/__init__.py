"""Backend-owned safe output locations and Artifact materialization."""

from .errors import (
    OutputArtifactNotEligible,
    OutputError,
    OutputIdempotencyConflict,
    OutputIntegrityError,
    OutputLocationUnavailable,
    OutputMaterializationFailed,
    OutputPolicyBindingMissing,
    OutputPolicyNotFound,
    OutputRevisionConflict,
    OutputRootChanged,
    OutputRootUnsafe,
    OutputValidationError,
)
from .models import (
    MaterializationProjection,
    MaterializationStatus,
    OutputAuditProjection,
    OutputLocationAlias,
    OutputLocationOption,
    OutputPolicyProjection,
    OutputPreferenceProjection,
)
from .repository import OutputRepository
from .service import OutputService
from .api import create_output_router
from .locations import standard_output_roots

__all__ = [
    "MaterializationProjection",
    "MaterializationStatus",
    "OutputArtifactNotEligible",
    "OutputAuditProjection",
    "OutputError",
    "OutputIdempotencyConflict",
    "OutputIntegrityError",
    "OutputLocationAlias",
    "OutputLocationOption",
    "OutputLocationUnavailable",
    "OutputMaterializationFailed",
    "OutputPolicyBindingMissing",
    "OutputPolicyNotFound",
    "OutputPolicyProjection",
    "OutputPreferenceProjection",
    "OutputRepository",
    "OutputRevisionConflict",
    "OutputRootChanged",
    "OutputRootUnsafe",
    "OutputService",
    "OutputValidationError",
    "create_output_router",
    "standard_output_roots",
]

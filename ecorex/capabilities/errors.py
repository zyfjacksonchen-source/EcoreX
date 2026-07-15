"""Stable capability-layer errors safe for API mapping."""


class CapabilityError(RuntimeError):
    code = "capability_error"


class CapabilityIntentError(CapabilityError):
    code = "capability_intent_invalid"


class DuplicateCapabilityError(CapabilityError):
    code = "duplicate_capability"


class UnknownCapabilityError(CapabilityError):
    code = "unknown_capability"


class CapabilityUnavailableError(CapabilityError):
    code = "capability_unavailable"


class CapabilityDeniedError(CapabilityError):
    code = "capability_denied"


class ApprovalRequiredError(CapabilityError):
    code = "approval_required"


class IdempotencyKeyRequiredError(CapabilityError):
    code = "idempotency_key_required"


class StaleCapabilitySnapshotError(CapabilityError):
    code = "stale_capability_snapshot"


class ToolHandlerMissingError(CapabilityError):
    code = "tool_handler_missing"


class ToolHandlerContractError(CapabilityError):
    code = "tool_handler_contract"


class ToolArgumentsValidationError(CapabilityError):
    code = "tool_arguments_invalid"


class ToolOutputValidationError(CapabilityError):
    code = "tool_output_invalid"

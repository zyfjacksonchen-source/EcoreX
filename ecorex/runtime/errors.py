"""Domain errors shared by repositories and the HTTP adapter."""


class RuntimeDomainError(Exception):
    pass


class NotFoundError(RuntimeDomainError):
    pass


class ConflictError(RuntimeDomainError):
    pass


class InvalidTransitionError(ConflictError):
    pass


class InteractionResponseValidationError(RuntimeDomainError):
    """A HITL response was rejected before any durable mutation."""

    pass


class IdempotencyConflictError(ConflictError):
    pass


class LeaseError(ConflictError):
    pass


class SchemaVersionError(RuntimeError):
    """The on-disk database cannot be opened by this runtime build."""

    pass

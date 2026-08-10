"""Memory-domain failures safe for API translation."""


class MemoryError(RuntimeError):
    code = "memory_operation_failed"


class MemoryConflict(MemoryError):
    code = "memory_request_conflict"


class MemoryResetNotFound(MemoryError):
    code = "memory_reset_not_found"


class MemoryUndoExpired(MemoryError):
    code = "memory_undo_expired"


class MemoryContentNotFound(MemoryError):
    code = "memory_content_not_found"


class MemoryContentUnavailable(MemoryError):
    code = "memory_content_unavailable"

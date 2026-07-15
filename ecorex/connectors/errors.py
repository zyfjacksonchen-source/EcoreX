class ConnectorError(RuntimeError):
    code = "connector_error"


class ConnectorNotFound(ConnectorError):
    code = "connector_not_found"


class ConnectorUnavailable(ConnectorError):
    code = "connector_unavailable"


class ConnectorAuthError(ConnectorError):
    code = "connector_auth_error"


class ConnectorPermissionDenied(ConnectorError):
    code = "connector_permission_denied"


class ConnectorInputInvalid(ConnectorError):
    """Caller input did not match the backend-owned action contract."""

    code = "connector_input_invalid"


class ConnectorIdempotencyRequired(ConnectorError):
    code = "connector_idempotency_required"


class ConnectorIdempotencyConflict(ConnectorError):
    code = "connector_idempotency_conflict"


class ConnectorInvocationUncertain(ConnectorError):
    """An external call may have happened and must not be repeated automatically."""

    code = "connector_invocation_uncertain"
    side_effect_uncertain = True

    def __init__(self, message: str, *, invocation_id: str | None = None) -> None:
        super().__init__(message)
        self.invocation_id = invocation_id


class ConnectorResultUnavailable(ConnectorError):
    """Provider completed but its result was safely rejected or unavailable."""

    code = "connector_result_unavailable"
    retryable = False
    side_effect_committed = True
    side_effect_uncertain = False

    def __init__(
        self,
        message: str,
        *,
        invocation_id: str,
        error_code: str,
    ) -> None:
        super().__init__(message)
        self.invocation_id = invocation_id
        self.result_error_code = error_code


class ConnectorReconciliationPending(ConnectorError):
    """Human decision is durable but provider execution has not quiesced."""

    code = "connector_reconciliation_pending"
    retryable = True

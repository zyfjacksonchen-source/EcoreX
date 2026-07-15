"""Managed Model Gateway errors with explicit retry semantics."""


class ModelGatewayError(RuntimeError):
    retryable = False


class GatewayUnavailable(ModelGatewayError):
    retryable = True


class GatewayProtocolError(ModelGatewayError):
    retryable = False


class GatewayAuthenticationError(ModelGatewayError):
    retryable = False


class GatewayRejected(ModelGatewayError):
    retryable = False

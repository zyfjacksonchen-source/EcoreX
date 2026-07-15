"""Fail-closed errors for the production Web Runtime boundary."""


class ServerConfigurationError(ValueError):
    pass


class BundleIntegrityError(RuntimeError):
    pass

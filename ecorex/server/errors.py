"""Fail-closed errors for the production Web Runtime boundary."""


class ServerConfigurationError(ValueError):
    def __init__(self, message: str, *, stage_code: str | None = None) -> None:
        super().__init__(message)
        self.stage_code = stage_code


class BundleIntegrityError(RuntimeError):
    pass

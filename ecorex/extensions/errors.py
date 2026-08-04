"""Typed failures for the backend-authoritative Extension Registry."""

from __future__ import annotations


class ExtensionError(RuntimeError):
    code = "extension_error"


class ExtensionManifestError(ExtensionError, ValueError):
    code = "extension_manifest_invalid"


class ExtensionVerificationError(ExtensionError):
    code = "extension_verification_failed"


class ExtensionCompatibilityError(ExtensionError):
    code = "extension_incompatible"


class ExtensionDependencyError(ExtensionError):
    code = "extension_dependency_invalid"


class ExtensionNotFound(ExtensionError):
    code = "extension_not_found"


class ExtensionRevisionConflict(ExtensionError):
    code = "extension_revision_conflict"

    def __init__(self, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ExtensionIdempotencyConflict(ExtensionError):
    code = "extension_idempotency_conflict"


class ExtensionActionUnavailable(ExtensionError):
    code = "extension_action_unavailable"


class ExtensionIntegrityError(ExtensionError):
    code = "extension_integrity_failed"


class ExtensionProviderRevoked(ExtensionError):
    code = "extension_provider_revoked"


class SkillStateChanged(ExtensionProviderRevoked):
    code = "skill_state_changed"


class SkillNotExecutable(ExtensionError):
    code = "skill_not_executable"

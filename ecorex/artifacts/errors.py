"""Domain-specific artifact errors safe for API-layer mapping."""


class ArtifactError(Exception):
    code = "ARTIFACT_ERROR"


class ArtifactNotFound(ArtifactError):
    code = "ARTIFACT_NOT_FOUND"


class RevisionNotFound(ArtifactError):
    code = "ARTIFACT_REVISION_NOT_FOUND"


class ArtifactNotUserVisible(ArtifactError):
    code = "ARTIFACT_NOT_USER_VISIBLE"


class ArtifactActionUnavailable(ArtifactError):
    code = "ARTIFACT_ACTION_UNAVAILABLE"


class ArtifactActionOutcomeUnknown(ArtifactError):
    """A non-replayable OS launch crossed the crash/response boundary."""

    code = "ARTIFACT_ACTION_OUTCOME_UNKNOWN"


class ArtifactExportFailed(ArtifactError):
    code = "ARTIFACT_EXPORT_FAILED"


class ArtifactLaunchFailed(ArtifactError):
    code = "ARTIFACT_LAUNCH_FAILED"


class ArtifactConflict(ArtifactError):
    code = "ARTIFACT_CONFLICT"


class IdempotencyConflict(ArtifactConflict):
    code = "ARTIFACT_IDEMPOTENCY_CONFLICT"


class RetouchConflict(ArtifactConflict):
    code = "ARTIFACT_RETOUCH_CONFLICT"


class ContentIntegrityError(ArtifactError):
    code = "ARTIFACT_CONTENT_INTEGRITY_ERROR"

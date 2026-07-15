"""Typed failures for the v0.3.0 to v1.0 copy-on-write migrator."""

from __future__ import annotations


class MigrationError(RuntimeError):
    """Base class for a migration that did not publish a target."""


class SourceLayoutError(MigrationError):
    """The selected source/target layout cannot be migrated safely."""


class SourceChangedError(MigrationError):
    """The source inventory changed while the migration was running."""


class LegacyDatabaseError(MigrationError):
    """A legacy SQLite database could not be read consistently."""


class LegacySchemaError(LegacyDatabaseError):
    """A required legacy table or column is missing."""


class DuplicateLegacyIdError(LegacyDatabaseError):
    """A legacy identifier has conflicting rows."""


class TargetConflictError(MigrationError):
    """A target already exists but is not this completed migration."""


class QuarantineKeyRequired(MigrationError):
    """Secrets exist but no external quarantine encryption key was supplied."""


class MigrationVerificationError(MigrationError):
    """The staged v1 database or CAS failed post-import verification."""


class QuarantineStateError(MigrationError):
    """The encrypted legacy-secret backup cannot be verified safely."""
